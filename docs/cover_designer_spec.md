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
- **Adjust layer** (§15) — a layer that owns no pixels of its own: it transforms the composite below it (grade, gradient map, bloom, vignette, blur, color wash), optionally through a mask.
- **Recipe** (§15) — a named finishing stack (config data): 5–10 adjust/texture/light layers appended above everything, including the type. How a spec reaches 20–30 layers without a model authoring each one.

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

### 6.3 ~~Critique pass~~ — REPLACED by the atelier (§16), 2026-08-31

The fixed critique-and-revise loop (a vision judge, a Sonnet reviser, up to four rounds) has been **removed**, along with `docproof/cover/critique.py` and the `reality.py` sample distillation that fed the old opening call. Both were bounded by their own shape: the judge could only ever ask its questions in the order it was written to ask them, and it had no move for most of what it found, so it reported defects it could not fix and spent its rounds on wall-clock pressure rather than on the cover being right.

What replaced it is §16: one director that reads the whole book and assigns each concept its spec, and one agent per concept that plans, buys, looks and iterates until it decides the cover is done. Everything §6.3 used to promise — a proof reviewed before it ships, art repainted when the pixels themselves are wrong, a design revised when they are not — the agent does with its own eyes and the composer's own verbs.

The §6.1 art-direction rule this section added still stands, and now lives in the director's prompt: prefer illustrated, painterly or graphic media for `art_prompts`; avoid photorealistic renders unless the brief explicitly calls for photography (stylized media hide generation artifacts; photoreal is the biggest "AI look" tell).

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
- **Per-job image tier (SHIPPED 2026-08-31).** 2K stays the default, but a job may instead be a *draft* job: `JobState.image_quality` is `"draft"` (1K, ~3¢) or `"full"`/`""` (2K, ~5¢ — what every job did before this field existed, and what any job.json missing it still reads as). The tier is chosen once on `POST /api/cover/jobs` (`image_quality` form field; anything else is a 422 with a sentence), stored on the job, and never revisable — a revision inherits it, so one job's ledger is quoted in one currency. `pipeline._image_tier(job)` is the single resolver: `_generate_art_slot` takes the tier as a required keyword argument and uses that same value both for `generate(resolution=...)` and for the `IMAGE_COST[...]` ledger row, so a cheap render can never be billed at full price or vice versa. This is the studio half of the ladder Cover Canvas already had (`canvas/regen.py`'s `DRAFT_TIER` + `finalize`): compose draft-first, then sharpen only the keeper.

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

When job creation includes a manuscript file: validate suffix/size exactly like `_read_upload` in [app/routes/quest.py](../app/routes/quest.py) (`.docx/.txt/.md`, 40MB cap, human-sentence errors), write it to a temp dir, run `read_sample_source` + `sample_text`, persist only the sample text to `manuscript_sample.txt` (never the full manuscript), and record `manuscript_name` and `word_count` on `JobState` (add both fields: `manuscript_name: str = ""`, `word_count: int = 0`). An unreadable file fails job creation with a 400 sentence — before any model spend. Revisions re-read the stored sample so grounding survives across versions.

**The director reads the whole book, and it is still never stored** (2026-08-31, §16). The route reads the uploaded file's full text in the same `with tempfile.TemporaryDirectory(...)` block that creates the job, hands it to `run_job(..., manuscript=text)` as an argument, and drops it when the job ends; `cover_pipeline.read_manuscript` is the one function that does this, so the policy stays in one place. What survives on disk is the director's `assignments.json` — its reading of the book plus one assignment per concept — never the prose. `manuscript_sample.txt` is unchanged and is the fallback the director reads when a job is replayed with no text in hand.

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

## 15. Deep-stack wave — the full layer engine (DRAFTED 2026-08-30, direction approved by owner; sub-decisions marked)

This section specs the wave against the CODE as it stands at v2.2 (widened slot ids, effects rack, texture shelf, patch-edit revisions, iterating critique), not against §4's launch-era snapshot — earlier sections are historical record; where they disagree with shipped code, the code wins.

### 15.0 Why: the census of a real cover PSD

A professional cover file is 20–30 layers, and the count is not more *content* — it is that most layers are **non-content**. Census of a typical trad-shelf jacket:

| Bucket | Typical count | What it is | Cover Studio v2.2 |
|---|---|---|---|
| Content | ~5 | background plate, figure, props, type | ✅ have (art slots, text slots) |
| Adjustment layers | 6–10 | curves/levels, gradient maps, color balance, selective saturation — usually masked. **What unifies an assembled collage into one image.** | ❌ none |
| Light & atmosphere | 4–6 | radial glows, rim light, fog banks between depth planes, light leaks, dodge/burn | ❌ none |
| Layer styles (clipped) | 2–5 per type layer | stacked shadows (tight dark + wide soft), inner shadow, glows, bevel, gradient/foil fill *inside* the glyphs | ⚠️ one shadow + one stroke |
| Masks | pervasive | gradient masks blending two plates into one scene; region-scoping a grade | ⚠️ alpha-from-another-slot only |
| Finishing group **over the type** | 3–4 | grain + vignette + global grade on top of everything — what makes type read as printed into the artwork, not pasted on | ❌ grain sits under text |

The wave closes those four gaps — plus five owner adds (2026-08-30): a **balance & symmetry engine** (§15.10 — "good symmetry / visually pleasing" has been the consistent live failure), **font-library expansion** (§15.11, superseding §14's deferral), **expressive typography** (§15.12 — let the model be inventive with text), a **masking doctrine** (§15.13 — machinery alone doesn't make the model reach for masks), and a **frontier composition planner** (§15.16, added mid-build — plan the multi-layer, multi-generation build ahead of any image spend instead of spontaneous one-shot direction). Design constraints, non-negotiable:

1. **Extend, don't rebuild.** `layers` is already an explicit bottom-first z-list that `render_upto()` replays deterministically; every new capability is a new layer kind or a new field, never a new render architecture.
2. **Byte-identical default path.** A spec that uses none of the new fields renders the exact bytes it rendered before the wave. Proven by a golden-bytes test (§15.10), same discipline as the per-category-passes wave.
3. **Pure Pillow, $0, deterministic.** No numpy, no new deps. Every op below is point-LUT / ImageChops / ImageMath / GaussianBlur arithmetic. Fixed seeds only (`_GRAIN_SEED` discipline).
4. **The schema is revision territory.** Everything lands as CoverSpec fields so §6.2 patch edits reach it for free. Wire safety: the revision wire schema is `SpecEdits` (tiny, unchanged) and the direction wire schema grows only one closed-Literal `recipe` field — no schema-size risk (the §6.2 grammar-compiler limit is about full-CoverSpec echo, which nothing here reintroduces).
5. **No nested groups (DECIDED in draft).** PSD-style layer groups are rejected: masks + clipped effects + recipes deliver ~90% of a group's value, and a flat list keeps `SpecEdits` dotted paths, `_layers_resolve`, and the whole validator family simple. Revisit only if a real cover proves impossible flat.

### 15.1 Blend modes

Widen every `blend` Literal (ArtSlot, and AdjustLayer below) from `normal|multiply|overlay|soft_light` to add: `screen`, `add`, `lighten`, `darken`, `color_dodge`.

- `screen`/`add`/`lighten`/`darken` are one-line `ImageChops` calls (`screen`, `add`, `lighter`, `darker`).
- `color_dodge` (the "light pops" mode — glows, leaks, foil glints) has no ImageChops op: implement per band via `ImageMath` (`min(255, a * 256 / (256 - b))`), converting to bands and merging. Deterministic; cost is three band evals at canvas size, milliseconds.
- `hue`/`color`/`luminosity` are **deferred** (per-pixel HSL math is where pure Pillow gets ugly; nothing in the recipe roster below needs them). Note the deferral in a comment where the Literal is defined.

Blending happens where it already happens (the one composite step in the layer walk); a blend-mode function table in the new `effects.py` (§15.9) replaces the current inline if/elif so art layers, adjust layers, and clipped overlays all share one implementation.

### 15.2 Masks as first-class

New model, attachable to any pixel-owning or adjust layer:

```python
class GradientMask(BaseModel):
    kind: Literal["linear", "radial"] = "linear"
    angle: float = 90.0            # linear: degrees, 90 = top-transparent→bottom-opaque
    center: list[float] = [0.5, 0.5]   # radial: fraction center
    start: float = 0.0             # fraction along the ramp where alpha begins rising
    end: float = 1.0               # fraction where alpha reaches 1.0

class MaskSpec(BaseModel):
    from_layer: str = ""           # an art slot id — that slot's POSITIONED alpha
    gradient: GradientMask | None = None
    luminance_of: str = ""         # an art slot id — that slot's positioned luminance as alpha
    invert: bool = False
```

- `ArtSlot` gains `mask: MaskSpec | None = None`. The existing `ArtSlot.mask_from` **stays** and folds into `mask.from_layer` at validation when `mask` is unset (sugar; old specs untouched — same fold pattern §15.4 uses for Shadow/Stroke). Setting both `mask_from` and `mask` is a validation error.
- `AdjustLayer` (§15.3) carries `mask: MaskSpec | None` — this is how a grade affects only the top third, how blur becomes depth-of-field.
- Combination rule when multiple sources are set: multiply them together; `invert` applies last. Validation: `from_layer`/`luminance_of` must name a real art slot; `from_layer` inherits `_mask_from_precedes`' ordering rule, `luminance_of` needs existence only (positioned pixels are computed up front — `_text_mask_from_resolves`' reasoning).
- Implementation: gradient masks are smooth by definition — synthesize at 25% scale and Lanczos-upsample, the `_GRAIN_SCALE` discipline. `start`/`end` remap the ramp (both 0..1, `start < end` validated).
- **Why this field earns its place:** a gradient mask on an art slot is how two plates blend into one scene (sky plate fading into texture plate — the collage move every full-bleed PSD uses); a gradient mask on a grade is a designer's most common adjustment gesture.

### 15.3 Adjust layers

New model + new `LayerRef.kind` `"adjust"` + `CoverSpec.adjust: list[AdjustLayer] = []`. An adjust layer owns no pixels: when the layer walk reaches it, compute `op` over the **current composite** and blend the result back through `mask` × `opacity`:

`result = composite × (1 − m·opacity) + op(composite) × (m·opacity)` — for `blend="normal"`; `color_wash` is the one op that composites a solid fill *as a layer* using the full §15.1 blend table instead.

```python
class AdjustLayer(BaseModel):
    id: str                        # _SLOT_ID_RE slug; shares the art-slot id namespace
                                   # (validator: no collision with any ArtSlot.id)
    op: Literal["grade", "gradient_map", "color_wash", "vignette", "bloom", "blur"]
    opacity: float = 1.0
    blend: Literal[...] = "normal" # §15.1 table; read by color_wash only
    mask: MaskSpec | None = None
    # -- flat per-op params (strict-schema rule: no dicts on any wire) --------
    brightness: float = 0.0        # grade: -1..1 → ImageEnhance factor 1+v
    contrast: float = 0.0          # grade: -1..1
    saturation: float = 0.0        # grade: -1..1 (ImageEnhance.Color)
    temperature: float = 0.0       # grade: -1..1 warm↔cool; linear R+/B- point LUT,
                                   # max shift ~±24/255 at |1| (exact constant
                                   # IMPLEMENTER'S CHOICE, comment it)
    stops: list[str] = []          # gradient_map: 2–3 entries, each a PaletteRole name
                                   # OR a #rrggbb hex; luminance → interpolated ramp
    color: str = ""                # color_wash/vignette ink: role name or hex; "" = scrim role
    strength: float = 0.5          # vignette/bloom amount, 0..1
    radius: float = 0.02           # bloom/blur: Gaussian radius as fraction of canvas height
    threshold: float = 0.75        # bloom: relative luminance above which pixels glow
```

Fields not read by the chosen `op` are ignored (validated-but-inert — deliberately forgiving so a patch edit changing `op` can't strand the spec in an invalid state).

Implementation notes, all pure Pillow:

- `grade`: `ImageEnhance.Brightness/Contrast/Color` with factor `1+v`; temperature via per-band `point()` LUTs.
- `gradient_map`: luminance ramp — convert composite to `"L"`, then `ImageOps.colorize(l, black=stop0, white=stop-1, mid=stop1-if-3)`. One line, and it is the single most cover-defining move in the whole wave (whole-composite duotones, teal-orange, sepia).
- `color_wash`: solid fill of `color` composited with `blend`+`opacity` (+mask). Covers dodge/burn painting when masked.
- `vignette`: full-canvas radial multiply toward `color`, `strength`-scaled — reuse the scrim vignette ramp math at canvas scope.
- `bloom`: threshold the luminance (`point`), blur at `radius`, `screen` back at `strength`. The "it's lit, not flat" op.
- `blur`: `GaussianBlur(radius)` selected through the mask (`Image.composite(blurred, composite, m)`) — with a gradient mask this is depth-of-field.

Validators: `_layers_resolve` extends to `kind="adjust"` (ref must name an `adjust` entry); `stops` length 2–3, each a valid role or hex; `radius`/`strength`/`threshold` ranges.

### 15.4 Effect stacks (layer styles)

Generalize per-layer styling from "one Shadow + one Stroke, text only" to an ordered stack on **both** TextSlot and ArtSlot:

```python
class Effect(BaseModel):
    kind: Literal["drop_shadow", "inner_shadow", "outer_glow", "inner_glow",
                  "bevel", "gradient_overlay", "texture_overlay", "stroke"]
    # flat params, same forgiving-fields rule as AdjustLayer:
    dx: float = 0.0; dy: float = 0.004   # shadows: fraction of canvas height
    blur: float = 0.006                  # shadows/glows
    color: str = ""                      # role or hex; "" = kind-appropriate default
                                         # (shadows #000000, glows accent role)
    alpha: float = 0.55
    width: float = 0.004                 # stroke/bevel depth, fraction of canvas height
    stops: list[str] = []                # gradient_overlay: 2–3 role-or-hex stops
    angle: float = 90.0                  # gradient_overlay ramp direction
    texture_file: str = ""               # texture_overlay: a TEXTURES shelf name (validated)
    blend: Literal[...] = "normal"       # overlays only
    opacity: float = 1.0                 # overlays only
```

- `TextSlot.effects: list[Effect] = []`, `ArtSlot.effects: list[Effect] = []`. **Same kind may repeat** — stacking a tight dark drop shadow under a wide soft one is the pro type move this whole subsection exists for.
- Back-compat fold: `TextSlot.shadow`/`stroke` stay as fields. When `effects` is empty, compose reads them exactly as today (byte-identical path). When `effects` is non-empty, a validator folds `shadow`→front / `stroke`→back of the stack so there is exactly one code path through the effects engine.
- **Paint order semantics** (fixed, documented in the model): the engine splits the stack into *under* effects (`drop_shadow`, `outer_glow` — painted beneath the layer's own pixels, in stack order) and *over* effects (`inner_shadow`, `inner_glow`, `bevel`, `gradient_overlay`, `texture_overlay`, `stroke` — applied clipped to the layer's alpha, in stack order, after the fill).
- Implementations (each a small function in `effects.py`, operating on a positioned RGBA layer):
  - `inner_shadow`: invert alpha, offset, blur, intersect with alpha (`ImageChops.multiply`), paint `color` at result.
  - `outer_glow`: blur the alpha, subtract the original alpha (glow lives *outside* the shape), colorize, composite under.
  - `bevel`: the cheap emboss — light copy offset toward the top-left edge of the alpha, dark copy toward bottom-right, both thin (`width`) and blurred; screen/multiply respectively, clipped to alpha.
  - `gradient_overlay`: build the ramp across the layer's alpha bbox at `angle`, `Image.composite` it over the fill through alpha, at `opacity`. **With metallic stops this is the foil title.**
  - `texture_overlay`: shelf plate cover-fit to the alpha bbox, clipped to alpha, blended at `blend`/`opacity` — grunge-in-the-letters, foil grain, linen type.
- The legibility autopilot's automatic busy-backdrop Shadow keeps writing to `TextSlot.shadow` (not `effects`) — the fold makes that composable with a designed stack instead of conflicting with it.

### 15.5 Light & atmosphere procedural bank

Widen `ArtSlot.procedural` with: `radial_glow`, `light_leak`, `fog_gradient`, `rays`, `bokeh`, `dust`, `scratches`, `stars`. These are ordinary art slots (usually `blend: screen|overlay|soft_light`, low opacity) — no new layer kind. Each synth:

- Extends the existing `_synth_*` signature to receive the ArtSlot, and reads **only** existing slot fields for parameters: `anchor` = center (glow, rays origin, fog band y), `scale` = size/extent, `opacity`/`blend` = as ever. No new per-synth param fields in v1 — the zero-param philosophy that keeps the procedural Literal a closed, judge-explainable menu.
- Is fixed-seed (`_synth_seed(version, slot_id, name)`, exactly like `speckle`), generated at ≤25% scale where smooth, full-res where crisp (`stars`, `scratches`).
- Palette-derived inks only: glow/leak from `accent` warmed toward white, fog from `background` lightened, dust/scratches near-`text`-color at low alpha. No new color fields.

`rim_light` behind a focal cutout is explicitly **not** a synth: it is `outer_glow` on the focal ArtSlot's own `effects` (§15.4) — one mechanism, not two.

### 15.6 Finishing recipes (`config/cover/recipes/*.yaml` + `docproof/cover/recipes.py`)

The force-multiplier: a **recipe** is a named, researched finishing stack that expands into real spec layers at `build_spec` time. This is how a spec reaches 25 layers with the model choosing one word.

```yaml
name: vintage_matte
describe: >-
  Sun-faded trade paperback: lifted blacks, warm wash, paper tooth, corner
  shading, soft grain. Literary, memoir, historical.
finish:                       # appended ABOVE the whole stack, text included, in order
  - adjust: {id: fx_lift,  op: grade, brightness: 0.05, contrast: -0.12, saturation: -0.10}
  - adjust: {id: fx_warm,  op: grade, temperature: 0.30}
  - art:    {id: fx_paper, texture_file: paper_tooth, texture_fit: tile, opacity: 0.12, blend: multiply}
  - adjust: {id: fx_vign,  op: vignette, strength: 0.28}
  - art:    {id: fx_grain, procedural: grain, opacity: 0.05, blend: overlay}
```

- **Expansion, not indirection (DECIDED in draft):** `build_spec` instantiates each entry through the real `ArtSlot`/`AdjustLayer` models, appends them to `spec.art`/`spec.adjust`, and appends their LayerRefs at the top of `spec.layers`. The spec stays fully self-contained — the archival guarantee ("spec + assets = pixels forever") never depends on a recipe file existing later, and §6.2 patch edits reach every expanded layer individually (the judge can say "halve fx_grain" and it is an ordinary one-field edit).
- The `fx_` id prefix is reserved for recipe-expanded layers (validator: hand-authored archetype slots may not use it), purely so humans and the judge can see at a glance which layers are finishing.
- `recipes.py` is a foundation-layer module (the `textures.py` pattern): loads YAML into `RECIPES: dict[str, dict]` with shallow checks (name/describe/finish present), exposes `describe_recipes()`. Deep validation happens at `build_spec` when entries hit the real models — a malformed shipped recipe fails its own unit test loudly. This ordering avoids a model.py↔recipes.py import cycle: model.py imports only the name list (for the closed `recipe` Literal via the `create_model` font trick).
- `Direction.recipe: str = ""` (closed Literal, `""` = none) — the art-direction call picks it. Archetype YAML may declare a default `recipe:` used when the direction stays silent; direction wins on conflict. Revisions may change `spec`-level layers freely; swapping the *whole* recipe post-build is expressed as ordinary layer edits (no re-expansion machinery in v1).
- **Roster (OPEN — owner review; ship 6–8, each grounded in `docs/cover_template_research.md` conventions rather than invented):** `vintage_matte` (above), `cinematic_duotone` (gradient_map on background+primary, bloom, grain — thriller/scifi), `dark_academia` (desaturate, warm temperature, heavy vignette, dust — romantasy/gothic), `airbrushed_glow` (bloom, radial_glow, +saturation — romance/romcom), `pulp_print` (halftone plate, hard gradient_map, speckle — retro/cozy), `midnight_neon` (cool grade, color_dodge radial_glow, bloom — scifi/urban), `quiet_literary` (whisper grain, −saturation, gentle vignette — the tasteful default), plus `""`.

### 15.7 Legibility autopilot × finishing (the one real interaction)

The autopilot samples the composite **at text-draw time**; a finishing stack above the text can afterwards change the contrast it approved. Required remedy, deterministic and bounded:

1. After the full stack renders, re-measure every text slot's contrast ratio against the **final** composite (same WCAG math, same thresholds).
2. A slot that now fails triggers, in order: (a) one more scrim-escalation replay pass (the `render_upto` machinery already supports "something changed, replay"); (b) the existing two-ink flip; (c) **finishing attenuation** — halve the `opacity` of `fx_`-prefixed layers above that slot, top-down, re-rendering after each, until the slot passes or every finishing layer is at ≤0.05.
3. Every attenuation lands in `RenderReport.warnings` (`"fx_grain halved to 0.03 to keep author line legible"`) — the judge then sees real measurements, per §6.3's `composer_warnings` channel.

The dead-band metric and the occlusion/contact guards keep running **before** the finishing group is applied — they measure design geometry, not grade, and a vignette must never mask a dead band from the metric.

### 15.8 Direction, revision, critique vocabulary

Moved: the owner adds (§15.10–15.13) each carry prompt vocabulary of their own, so the model-call changes are consolidated in **§15.14** rather than split across two sections.

### 15.9 File plan & engineering constraints

| Path | Change |
|---|---|
| `docproof/cover/effects.py` | **New.** Pure pixel ops, no opinions: the blend-mode table, mask synthesis, the six adjust ops, the eight layer-style effects. Imports model.py, never compose.py (typeset.py's own rule). |
| `docproof/cover/recipes.py` | **New.** Foundation-layer YAML loader (§15.6). |
| `docproof/cover/model.py` | `MaskSpec`, `GradientMask`, `AdjustLayer`, `Effect`; new fields on ArtSlot/TextSlot/CoverSpec/Direction; validators (§15.2–15.6). |
| `docproof/cover/compose.py` | Layer walk gains `kind="adjust"`; effect-stack invocation around text/art painting; final-composite legibility re-check (§15.7); blend table moves out to effects.py; calls the balance pass (§15.10). |
| `docproof/cover/balance.py` | **New.** The balance & symmetry engine (§15.10): axis snap, rail snap, mirror/mass/margin/rhythm measurements. Imports model.py only. |
| `docproof/cover/typeset.py` | §15.12: justify_stack fit, arc baselines, rotation, emphasis runs. |
| `docproof/cover/fonts.py` + `config/cover/fonts/` | §15.11: registry grows role tags + style companions; new cover-owned font directory (prep's 10 stay where they are and stay registered). |
| `docproof/cover/direction.py`, `critique.py` | §15.14 prompt additions. |
| `config/cover/recipes/*.yaml` | The roster (§15.6). |
| `config/cover/archetypes/*.yaml` | Retrofit 2–3 (IMPLEMENTER'S CHOICE which, e.g. thriller + romantasy + full_bleed_art) with designed effect stacks and a default recipe; every archetype gains an `axis` declaration (§15.10); 1–2 new mask-forward templates (§15.13). |
| `pyproject.toml` | package-data: `"config.cover.recipes" = ["*.yaml"]`, `"config.cover.fonts" = ["*.ttf", "*.txt", "*.md"]`. |
| `docproof/__init__.py` | Version bump per PR. |

Memory (512MB box): the walk stays in-place — one composite buffer mutated layer by layer; an adjust op holds at most 2–3 transient full-canvas buffers (~16MB each at 1600×2560 RGBA); effect stacks render per layer and free before the next. A 30-layer spec peaks well under 100MB; the compose asyncio.Lock (§7.4) already serializes renders. Masks and smooth synths generate at 25% scale.

### 15.10 Balance & symmetry engine (`docproof/cover/balance.py`)

The consistent live failure: covers that are *almost* right — a title 2% off the center axis, two left-aligned blocks on slightly different rails, one half of the canvas visibly heavier. Humans read these instantly as amateur; no prompt fixes them reliably. So this is code, in the house shape: **measure the current canvas, snap what's snappable, report the rest as numbers the judge can act on.**

**Axis declaration.** Every archetype YAML gains `axis: Literal["center", "left", "right"] = "center"` (fraction rail via `axis_x: float` for left/right, default 0.08/0.92). `build_spec` copies it onto the spec (`CoverSpec.axis`, `axis_x`) so revisions can change it.

**Snap pass** (deterministic, runs after positioning, before finishing):

- *Axis snap:* for every text slot and every contain-fit art layer, measure the ink-bbox center (center axis) or edge (left/right rail). Within **1.5% of canvas width** of the axis but not exactly on it → translate the layer onto it exactly. Off by more than that, leave it — it reads as intentional asymmetry, and §15.10's job is killing near-misses, not enforcing centering.
- *Rail snap:* collect the leading-edge insets of all same-aligned text slots; any pair within 1.5% of each other but unequal snaps to the topmost slot's rail. Same for trailing edges of right-aligned sets.
- *Gap rhythm:* measure vertical ink gaps between adjacent stacked text slots; two gaps within 20% of each other but unequal get a warning only (`"title→subtitle gap 3.1%, subtitle→author 3.8% — consider equalizing"`). No auto-move in v1: vertical position interacts with zones, scrims, and the occlusion guards, and a wrong vertical snap is worse than a reported near-miss.
- Every snap is logged to `RenderReport.warnings`-adjacent info (a new `RenderReport.adjustments: list[str]`) so "why did it move" is never a mystery.

**Balance measurements** (reported, never auto-fixed — these are taste calls the judge arbitrates):

- *Mirror symmetry score:* mean abs luminance difference between the composite and its horizontal flip, inverted to 0..1. Reported always; for `axis="center"` specs scoring < 0.55, a warning names the heavier half by ink mass (`"right half carries 63% of visual weight"`).
- *Visual center of mass:* luminance-weighted centroid; warn when horizontally > 6% off the axis.
- *Margin audit:* min ink distance to each canvas edge per element; warn on any element closer than 2% to a trim edge (unless it is a bleed layer — `fit="cover"` art is exempt).

All of it flows into §6.3's `composer_warnings` channel, and the judge prompt (§15.14) gains balance tells — the judge stops eyeballing symmetry and starts reading measurements, exactly the `composer_warnings` doctrine that fixed the legibility loop.

**Tests:** a slot 1% off-center snaps and the adjustment is recorded; a slot 5% off-center does not; two rails 0.8% apart unify; symmetry score is 1.0 for a mirrored fixture and low for a lopsided one; heavier-half attribution correct on a constructed fixture; exempt cover-fit art doesn't trigger margin warnings.

### 15.11 Font library expansion (`fonts.py` + `config/cover/fonts/`) — supersedes §14's deferral

Ten families cannot cover ten genres' shelf conventions. Grow to **~30 families**, all OFL/Apache with unrestricted embedding, vendored (self-hosted TTFs, no network at render time — ~5MB total, fine for the wheel).

- **New home:** `config/cover/fonts/` (cover-owned; prep's 10 under `config/prep/fonts` stay registered — the registry reads both roots). License texts ship alongside the TTFs; a `README.md` maps family → file → license, mirroring prep's.
- **Registry model grows:**

```python
@dataclass(frozen=True)
class CoverFont:
    family: str; file: str; vibe: str; caps_friendly: bool
    role: Literal["display_serif", "didone", "slab", "sans", "condensed_caps",
                  "script", "blackletter", "mono", "decorative", "small_caps"]
    italic_file: str = ""          # style companion when the face ships one
    bold_file: str = ""
    pairs_with: tuple[str, ...] = ()   # suggested author-line partners, fed to §6.1
```

- **Candidate roster** (IMPLEMENTER'S CHOICE final cut after verifying each license + embedding bits at implementation time; these are the shelf-convention anchors): *condensed caps* — Bebas Neue, Anton, Oswald, Archivo Black (thriller/nonfiction big-type); *didone/display serif* — Abril Fatface, Playfair Display SC, Rozha One, Yeseva One, Libre Caslon Display (literary/romance prestige); *slab* — Alfa Slab One, Zilla Slab (middle-grade/cozy); *script* — Great Vibes, Sacramento, Dancing Script (romance/romcom); *engraved/small-caps* — Cinzel, Marcellus, Julius Sans One (historical/fantasy); *decorative* — Monoton (neon scifi), Rye (western), UnifrakturMaguntia (gothic/horror, judge-vetoed for camp elsewhere); *workhorse sans* — Fjalla One, Archivo. The direction prompt's closed Literal grows automatically (it is built from the registry).
- **Prompt integration:** `describe_fonts()` now groups by `role` with the vibe lines, and states pairing hints. The §6.1 rule "picks from that closed list" is unchanged — more choices, same guardrail. The §6.3 judge gains one tell: *typeface fights the genre's shelf* (a script-titled thriller, a Bebas romance).
- **Tests:** every registered file exists and loads in Pillow; italic/bold companions load; the Literal rebuild picks up the full roster; scratch-venv package-data proof for the new directory.

### 15.12 Expressive typography — inventive text, one move at a time

Today every title is horizontally set, uniformly sized, straight-baselined. Real covers earn their look with a small set of type moves. Add the four that dominate trad shelves, as TextSlot fields (all revision-editable), with the **one-signature-move rule**: a fresh direction may request at most ONE move per concept (`build_spec` enforces; revisions may do as they're told). Restraint is what separates these moves from effect soup.

```python
class TextSlot(...):
    ...
    fit_mode: Literal["uniform", "justify_stack"] = "uniform"
    arc: float = 0.0               # -0.35..0.35; + = arch (upward bow), − = valley
    rotate: float = 0.0            # -15..15 degrees, whole-slot tilt
    emphasis: list[int] = []       # word indices (post-case split) styled differently
    emphasis_style: Literal["accent_color", "italic", "swap_face", "larger"] = "accent_color"
    emphasis_font: str = ""        # swap_face only; validated against FAMILIES
```

- **`justify_stack`** (the poster stack — nonfiction/thriller's backbone): instead of one size for all lines, each line is sized independently so every line's tracked width fills the zone width exactly, subject to `size_min`/`size_max` and a max 2.8× ratio between smallest and largest line. Candidate line-breaks are scored by minimal wasted vertical space instead of width variance. "THE" alone on a line rendering huge is *correct* here — cap the ratio, not the drama.
- **`arc`**: per-glyph placement along a circular baseline bowed by `arc` × zone height; glyphs rotate to the local tangent. Fit search measures the arc's chord. (Pillow per-glyph rotation — deterministic, already glyph-by-glyph when tracking is on.)
- **`rotate`**: render the slot flat, rotate with expand, re-anchor in the zone. The legibility sample and every ink-based guard use the rotated bbox (they already measure real ink, so this is mostly free).
- **`emphasis`**: style runs at word granularity — the "and" in italic accent, the key noun in the accent color, one word `larger` (1.25×). `swap_face`/`italic` require the face (companion file or `emphasis_font`) to exist — validator, not runtime surprise.
- **Direction-time access:** `Direction` gains `type_move: Literal["", "justify_stack", "arch", "tilt", "emphasis"]` — one word, mapped by `build_spec` onto the title slot with safe parameters (arch → `arc=0.18`; tilt → `rotate=-6`; emphasis → model also supplies `emphasis_word: str` matched case-insensitively to a title word, dropped with a log line if absent — the §6.1 surplus-prompt precedent). Raw fields stay archetype/revision territory.
- **Guards that already exist keep working** because they measure ink, not zones: occlusion, contact, dead-band, autopilot. New test fixtures cover each move under each guard.
- **Tests:** justify_stack line widths within 1px of zone width; ratio cap honored; arc chord fits; rotated ink stays inside canvas; emphasis run color/face assertions; one-move rule rejects a direction requesting two; every move × 100px thumbnail legibility on a fixture brief.

### 15.13 Masking doctrine — making the model actually reach for masks

§15.2 builds the machinery; this section makes it *used*. Three parts:

1. **One more mask source — text as clip:** `MaskSpec.from_text: str = ""` — clip an ART layer to a text slot's fitted glyph alpha (photo-in-the-letters as a first-class art move, complementing `TextSlot.mask_from` (text clipped to art) and `mode="art_fill"` (glyphs as a window)). Feasible because compose resolves text ink before positioning art (the occlusion guard already depends on that ordering). Validator: names a real text slot; the text slot must not itself be `mask_from`-clipped to the same art (cycle check).
2. **Direction-time mask intents** (closed, safe, tiny — full MaskSpec freedom stays archetype/revision territory): `ArtPrompt.mask_intent: Literal["", "blend_into_background", "inside_title", "inside_focal"] = ""`. `build_spec` maps: `blend_into_background` → a linear gradient mask on this slot angled toward the background plate (the two-plate collage move); `inside_title` → `mask.from_text="title"`; `inside_focal` → `mask.from_layer` on the archetype's focal slot. The §6.1 prompt names all four masking moves — plate-blend, text-in-thing, thing-in-text, region-grade — each with one sentence of *when it earns its place*, and requires transparent-cutout prompts for `inside_*` sources when the target is a cutout.
3. **Mask-forward archetypes:** ship two reference templates — `title_window` (big `art_fill` title over a color field, art visible only through the glyphs + a quiet finishing recipe) and `split_plate` (two generated plates gradient-masked into one scene, type on the seam). Templates are how conventions propagate: the judge sees them pass, revisions imitate them.

**Tests:** from_text clip determinism + cycle rejection; each mask_intent expands to the documented fields; both new archetypes procedural-render green through the autopilot and balance pass.

### 15.14 Direction, revision, critique vocabulary (consolidated)

- **§6.1 (direction):** enumerate recipes with `describe` lines; the grouped-by-role font roster (§15.11); `type_move` with the one-move rule stated; `mask_intent` with the four moves and when each earns its place; recipe-for-the-genre guidance (big_type usually wants `quiet_literary` or nothing). The model still never sets adjust-layer/effect fields directly at direction time — recipes, `type_move`, and `mask_intent` are its whole vocabulary there (§7.4a doctrine, extended).
- **§6.2 (revision):** the patch grammar reaches every new field. Add worked examples: *"warmer and moodier"* → `adjust[i].temperature` + vignette `strength`; *"type feels pasted on"* → `layers` whole-list replace moving `fx_` layers above the text; *"make the title a stacked poster title"* → `text[0].fit_mode`; *"put the forest inside the title"* → `art[k].mask.from_text`.
- **§6.3 (critique):** new tells, all measurement-backed where possible: *type reads pasted-on*; *flat unlit composite*; *filter soup*; *left/right visibly unbalanced* (the judge receives the §15.10 symmetry score and heavier-half attribution via `composer_warnings` and must cite the number when it flags this); *near-miss alignment survived* (should be impossible post-snap — flagging it means a balance-pass bug, so the warning text says exactly that); *typeface fights the genre's shelf*; *gimmick without payoff* (a type move or mask that hurts legibility or hierarchy — recommend removal, it's one patch edit).

### 15.15 Build order & tests

Six PRs, each independently shippable and green (fonts and balance parallelize with the engine):

1. **PR1 — engine.** §15.1 blends + §15.2 masks (incl. `from_text`) + §15.3 adjust layers + §15.7 re-check. Tests: golden-bytes back-compat (compose every shipped archetype procedurally before/after → identical bytes); each blend mode against hand-computed 2×2 fixtures; mask combination/invert/ordering/cycle validation; each adjust op deterministic on a fixed composite; gradient_map output contains only ramp colors; the re-check attenuation ladder end-to-end.
2. **PR2 — balance & symmetry.** §15.10, its own tests. Independent of PR1 (operates on positioned ink, not new layer kinds).
3. **PR3 — fonts.** §15.11. Independent.
4. **PR4 — styles + atmosphere + recipes.** §15.4 + §15.5 + §15.6 + archetype retrofits. Tests: shadow/stroke fold equivalence (old fields alone → byte-identical); stacked double shadow; every synth deterministic + palette-coherent; every recipe expands valid and its procedural render passes autopilot + balance; `fx_` collision rejected; scratch-venv package-data.
5. **PR5 — expressive typography.** §15.12 with its per-move × per-guard fixture matrix.
6. **PR6 — vocabulary.** §15.14 prompts + the two mask-forward archetypes + worked examples + judge tells. Prompt-shape assertions plus the live acceptance run.
7. **PR7 — composition planner.** §15.16 (after PR6 — it consumes the full finished vocabulary: recipes, type moves, mask intents, effect stacks). Tests: plan schema round-trip; staged-generation ordering honored with a fake imaging client; conditioning review receives the prior stage's actual bytes; planner failure degrades to the spontaneous path with a ledger note and no lost job; plan.json persisted and replayable; consistency-contract suffix present in every staged prompt.

Acceptance (wave): (a) same brief, `recipe=""` vs `cinematic_duotone` — visibly graded, unified, zero image-spend delta; (b) "make it feel more printed / less digital" resolves to `fx_` edits alone; (c) golden-bytes holds on every pre-wave fixture; (d) a foil title (gradient_overlay + texture_overlay) survives the 100px thumb check; (e) a deliberately 1%-off-center fixture ships exactly on axis with the adjustment logged; (f) a `justify_stack` + `title_window` concept renders legibly at 100px; (g) the judge's balance flag cites the measured score; (h) full suite green.

### 15.16 Composition planner — a frontier model that plans before pixels (OWNER ADD 2026-08-30)

The pipeline's one remaining spontaneous step is the most consequential: §6.1 fires one cheap breadth call and every image prompt it emits is written blind — no prompt knows what the other generations will look like, so nothing guarantees the layers were conceived as ONE composition. Good covers are a combination of multiple layers and multiple image generations; a designer plans that combination before making anything. This section adds that planning mind, on a frontier model, between choosing a concept and spending image dollars.

**Where it sits.** `run_directions` stays exactly as is (cheap, N distinct concepts — breadth is the wrong place for expensive depth). The planner runs per concept, after direction, before painting: `plan_composition(brief, spec, archetype, manuscript_sample) -> CompositionPlan` in `docproof/cover/planner.py`. Default model: a frontier reasoner — module constant `PLANNER_MODEL = "claude-fable-5"` (fallback `claude-opus-5`), called via the `anthropic` SDK directly with vision, the same vendor-SDK-in-its-own-module precedent critique.py set. Cost note: ~$0.10–0.30 per concept including staged reviews — cite current pricing in a comment at implementation time. **Lane doctrine respected:** the shipped pipeline gates the planner behind `COVER_PLANNER` env/setting (off = today's spontaneous path, byte-for-byte); in the $0 lane the Claude session itself plays this role following this same contract, which is what makes recipes learned there portable back into the product.

**What a plan is** (`CompositionPlan`, strict-schema flat like everything else on the wire):

- `light`: one shared lighting contract — key-light direction, quality, time of day — injected verbatim into EVERY art prompt (the single biggest "these layers belong together" lever).
- `palette_anchors`: the exact hexes each generation must name, drawn from the spec's palette.
- `depth`: per-slot plane (far/mid/near), a shared `horizon_y`, and where negative space must fall — computed FROM the text zones by code, but restated by the planner in prompt language the image model obeys.
- `generation_order`: slot ids as sequential stages (a plate that others must match generates first).
- `conditioning`: per later-stage slot, which earlier slot's *actual render* the planner reviews (vision) before finalizing that slot's prompt and placement fields — anchor, scale, offset, mask — so the focal is prompted and positioned against where the background's negative space and horizon *really* landed, not where the plan hoped.
- `unify`: the finishing bind — recipe choice and/or a gradient_map/grade the planner wants over the assembled stack.
- Per-slot rewritten prompts, each ending in the shared consistency suffix (light + palette + era + medium), on top of the §7.2 negative suffix.

**Staged generation.** When a plan declares `generation_order`/`conditioning`, `pipeline.run_job`'s painting phase runs those stages sequentially (bounded: ≤3 stages, ≤1 vision review per stage; everything else in a stage still parallelizes under the existing semaphore). Each review is one structured call: images of prior stages (≤600px, critique.py's discipline) + the plan + the pending slot's draft prompt → final prompt + placement + mask fields for that slot. Ledger rows `{kind: "plan", detail, usd}` per planner/review call.

**Guarantees.** `plan.json` persists in the job dir beside `job.json` (replayable, auditable). Planner or review failure NEVER blocks a cover: log, ledger note, fall back to the spontaneous path for the remaining slots. Revisions may request a replan (`allow_new_art` + explicit "replan" in notes → planner reruns before regeneration); ordinary revisions never re-buy planning. The critique judge receives `plan.light` and `plan.unify` in its summary so it can name plan-vs-render drift as a tell.

**Why this and not a bigger direction call:** breadth and depth want different models and different token budgets; planning all N concepts at frontier depth before a human (or the judge) has culled them wastes most of the spend. Direction proposes; the planner engineers the winner.

### 15.17 Out of scope (this wave)

Nested layer groups (§15.0, constraint 5); `hue`/`color`/`luminosity` blends; PSD import/export; hand-painted dodge/burn (masked grades cover it); per-synth parameter fields; recipe re-expansion on revision; curves as arbitrary control-point LUTs (grade's four scalars first — add curves only if the judge demonstrably runs out of range); vertical gap auto-equalization (warn-only in v1, §15.10); variable-font axes (static TTF weights only); per-glyph manual kerning overrides; text-on-arbitrary-path (circular arc only).

### 15.18 Element inspection kit — "pixel-perfect" as a procedure (docproof/cover/inspect.py)

Born from the first real $0-lane cover (Willow On Me, 2026-08-30), where three consecutive
seatings of a generated figure failed BY EYE and every defect that actually got fixed was
fixed off a measurement. The doctrine, one line: **no claim about where pixels sit is made
by eye; every claim is made against a ruled artifact or a numeric probe.**

The concrete per-element pass, run between `compose()` and the next spec patch (the same
seat at the table RenderReport holds for legibility):

1. **Audit every asset** — `audit_assets(spec, job_dir)`: per asset slot, `ink_bbox`'s
   raw-vs-hard-alpha bbox and the haze padding each side. A generated cutout's `getbbox()`
   lies (~180px of near-invisible haze floated the Willow figure); anything seated by a raw
   bbox floats by its bottom haze. Flagged entries are seated by the `hard` box.
2. **Isolate anything suspicious** — `isolate(spec, slot_id, job_dir)`: the layer rendered
   alone through compose's own placement path (never a re-implementation).
3. **Measure the ground, never guess it** — `ruled_crop` (a crop with a coordinate grid
   ruled on in source coordinates) to orient; `surface_line` (per-column strongest
   dark-to-bright edge inside a TIGHT y-band) for the actual surface polyline. A loose
   band locks onto background structure — constrain it to the expected surface.
4. **Verify the seat as numbers** — `contact_gaps(surface, contacts)`: per contact point,
   surface_y − contact_y. Seated means every gap in [−15, +2] (a few px sunk is wanted).
   Contact shadows go under the CONTACT POINTS — one body-wide bar reads as a chasm.
5. **Scan plates for banding, confirm before fixing** — `seam_scan` (stripe-swept
   column-mean step detector) runs on the GENERATED PLATE only: a finished composite's
   legitimate vertical content (glyph stems, spires, arcs) out-fires subtle banding by an
   order of magnitude, so composite hits are noise. Every hit is a pointer, convicted only
   by `column_profile` (a step between flat flanks = seam; a slope = content) and a
   `ruled_crop`. The fix for a confirmed seam is strip re-synthesis (row-wise lerp between
   flanks, dodging painted features) — a luminance step survives any blur.

Integration companions (the seat is only half the illusion): re-paste the plate's own
surface material over the element's lowest pixels (a polygon-masked snow/ground lip) so it
sinks IN rather than sits ON; add accent bounce light and disturbance (spray) at the
contact; scale the element to the surface's own depth cues, not to taste.

Like balance.py, everything is a pure value computation over supplied images — no network,
no state, deterministic. `isolate()` alone touches compose, precisely so isolation can
never disagree with the real render.

### 15.19 One-shot doctrine — from the first real cover to "upload a manuscript, get 4-6 bangers"

Provenance: the first end-to-end real cover (Willow On Me, 2026-08-30) — one manuscript read,
three routes (painterly ×9 versions, in-engine stylized ×4, user-generated stylized elements ×16),
one shipped v16. Nearly every iteration was BUG-driven, and every bug below is now a rule, a probe,
or a test. The residual per-cover loop is the 2-3 judgment rounds §6.3's critique already supports.
That is the whole thesis of this section: **one-shotting is not better luck, it is doctrine baked
into code plus gates that refuse the known failures.** Target user: a non-technical author who
uploads a manuscript and gets back 4-6 covers that do not read as AI.

**Design doctrine (the taste rules, learned the hard way)**

1. *Dead space is the enemy.* `dead_band_frac > ~0.2` is a FAIL GATE, not a warning. The engine
   flagged the failure (40% empty sky) before the owner did; the operator overrode it. Never again.
2. *The hero silhouette carries the story; rendered detail reads as AI.* A crouched near-black
   shape with a trail entering her back is a sentence; a fully painted figure is "a generation."
   Figure at 35-50% of cover height. Scale is hierarchy-honest, never perspective-honest.
3. *Signature marks need story-physics.* A uniform glowing stroke "looks like a line." The
   teleport-trail only worked as TRAVEL HISTORY: a multi-hop chain between real plate landmarks,
   landing flash at each vertex, intensity graded oldest-faintest → newest-boldest.
4. *Every element needs a reason on the page.* Worldbuilding props earn their place by double
   duty (the airship became the series-line emblem). Decorative occluders with no story job die
   in review ("why are those flowers there?").
5. *Type has homes.* Series line = eyebrow above the title, optionally with an ornament. Author
   line = on a PAINTED stable ground (a band, a cast shadow) — never scrims fighting busy texture
   (a panel scrim at escalated strength reads as a bezel; `gradient_up` darkens everything ABOVE
   its zone and can black out the cover). Title may interleave with art via layer order (§15.13).
6. *Stylized flat/screen-print hides AI tells; painterly exposes them.* Grain reads as intent.
   Flat style + shared palette hexes is also what makes separately generated elements cohere.
   The product should DEFAULT to 2-3 stylized looks.

**Element decomposition (the generation architecture)**

Generate MATERIALS and SUBJECTS; paint GEOMETRY deterministically — anything whose endpoints must
hit measured pixels (trails, frames, stage bands) is Pillow's job, textured by generated plates
via luminance×mask clipping (cap the mask ~0.5 or bright texture whitens the shape).

- one scene plate (no people, no text; landscape rescue = tone-matched procedural sky extension,
  feathered seam, patch baked-in moons);
- one figure cutout (transparent; silhouette-first prompt);
- one energy/material texture on PLAIN BLACK (for luminance masking);
- optional props (emblem, occluder) — each with a story job or not at all.

Every prompt carries the SAME style block + palette hexes + lighting contract (§15.16's planner
already owns this injection). Plates arrive with gifts (painted lanterns → chain vertices; a glow
→ an origin) and defects (vertical banding a blur cannot remove — a step survives blur;
re-synthesize the strip as row-wise lerp between flanks). Plan to exploit the gifts: the vision
review stage places elements against the ACTUAL render.

**Integration checklist (a pasted element is seated, not floated — §15.18's kit, applied)**

seat by hard-alpha bbox (raw bbox haze floats figures) → find the surface with ruled grids and
surface_line, never by eye → contact-POINT shadows (a body-wide bar reads as a chasm) → re-paste
the surface material over the lowest pixels → shared-light rim synthesized from the scene's key
light → disturbance (spray, bounce pool) → verify seat as numbers (contact_gaps in [-15, +2]).

**Pixel traps (each cost a version; all now doctrine)**

- PIL `paste(im, box, mask)` REPLACES pixels — stacking translucent layers requires
  `alpha_composite` of masked layers, or the underprint silently vanishes.
- Glow vocabulary INVERTS on light grounds: a pale core ≈ snow = invisible. On light backdrops
  the mark is saturated body + dark underprint edges, no light core.
- Silent `str.replace` edits no-op on stale targets; spec/scripted edits must assert their match.
- `gradient_up` scrim = everything above the zone darkens (see 5).
- Luminance-masked texture fills whiten their shape unless alpha-capped.

**The one-shot build list (each item PR-sized; §15.16/§6.3/§15.18 are the substrate)**

1. *Manuscript → visual brief*: extend the reality distiller to extract the OWNABLE MARK (the
   green-arc equivalent), hero pose/silhouette description, three landmarks, setting palette,
   series name, comp titles.
2. *4-6 composition templates as archetypes*: hero-silhouette-over-scene (the v16 layer program,
   parameterized), emblem/crest, inverted-values, split-world, big-type, typographic. Zones,
   layer order, type homes, and integration steps encoded — concepts differ by template +
   palette, not by luck.
3. *Planner upgrade*: element decomposition in the CompositionPlan (plate → cutouts → materials
   as staged generation), style block + palette anchors in every prompt, review stages that
   place elements against actual renders and exploit plate gifts.
4. *integrate.py*: promote the job-script moves into the engine — seat_figure (hard-alpha +
   surface detection + auto contact shadows from mask lows), rim/silhouette synthesis, and a
   signature-mark painter (chain/bolt renderer whose color vocabulary flips on light grounds).
5. *Gates*: dead_band, audit_assets haze, plate seam_scan, mark-visibility (path-vs-background
   delta), plus new critique tells: floating figure, line-reads-as-line, element-without-reason,
   painterly-AI-sheen.
6. *Style lock + economics*: default stylized; ~5 elements × $0.05 × 5 concepts + planner/critique
   ≈ under $3 per book for 4-6 covers, generated in-product (authors never see a prompt), every
   cover an archived spec that re-renders byte-identically and revises for $0.

### 15.20 Arrangement doctrine — scene agreement (the Desaturated addendum)

Provenance: the second real cover (Desaturated, 2026-08-30) — 23 versions across three
generations of the same scene (white-ground flood-cut elements → native-alpha elements →
same elements REARRANGED), converging only when a hand-arranged reference built from the
IDENTICAL assets outperformed every engine composite. The lesson that closed the gap:
**every "wonky" render was a violated relationship, not a wrong coordinate.** §15.19
seats one element; this section makes the elements agree with each other. The compressed
form: *a cover scene is a claim about one world — one viewpoint, one light, one ground —
and every element must be provably inside that claim before any element is polished.*

**The framing (owner, 2026-08-31). Read this before the rules; it is what they are all
instances of.** *A book cover is composed of separate transparent plates that come
together to tell a story, and the text and the colour are telling that same story.* Two
consequences, and between them they are this whole section in miniature:

- *Type and palette are claims about the world, not decoration over it.* A typeface and a
  colour are as load-bearing as a plate — they say what kind of place this is, what year
  it is, who is talking. A face or a hue that belongs to a different story is as broken as
  a floating figure; it is simply harder to name, so it survives review and the cover just
  reads "off". Choose them from the story, and check them against the same one world the
  plates are claiming.
- *The loudest defect a reader can hit is an impossible physics interaction.* Anything
  that makes someone stop and think "…wait, what's that?" is almost always two plates
  disagreeing about the physical world: something standing on nothing, a shadow with no
  source, an object clipped by a thing it sits in front of, a near band and a far band at
  one value. Readers cannot articulate any of it and do not need to — they just quietly
  distrust the cover and move on. This is why the gates below are numeric and why they run
  on every delivered version: the defect is invisible to the person who built the scene
  and instant to everyone else. Every numbered rule that follows is one such interaction,
  caught once, in production.

**Scene-agreement rules (owner-bound, each learned as a failure)**

1. *Ground-level agreement.* A scene agrees on ground level in exactly one of two modes:
   SHOW a floor and seat every element on it with matched perspective (§15.18/§15.19
   machinery), or HIDE the floor and conceal EVERY ground contact — no foot, leg-end, or
   base visible against sky, cloud, or canopy. Never mix modes. A waist-deep-field cover
   is mode two: the reference showed not one termination anywhere, and each of ours that
   showed (post tip in a cloud gap, boots on the leaf canopy) read as a float regardless
   of shadows.
2. *Arrangement before assets.* Design the depth-band stack first (sky → deep row →
   structure → mid figures → front band), commit to one viewpoint scale ("the viewer
   stands in the rows"), and plan universal overlap — no element keeps its complete
   outline. Unbroken silhouettes are THE composited-cutout tell. Then derive asset briefs
   from the arrangement, requesting structures already at their immersion depth (a
   billboard waist-deep in corn), not engineered into it afterward.
3. *The third-variable rule.* When two placement constraints cannot be reconciled by
   moving the element (legs must bury vs. sky must not empty), the answer is a variable
   neither constraint names — here field density (add a deep row behind the structure) —
   never procedural limb surgery. Tiled/sheared texture extensions read as a checkered
   ladder; the vetoed fix cost five versions.
4. *One plate per depth band; tiling within a band is legal.* Foreground occluder and
   background field are separate generations (never re-slice one plate across bands), but
   a band wider than its plate may repeat the plate flipped/shifted, and clusters may be
   cropped from a band's own plate and re-aimed at specific burial points.
5. *Coherence is arrangement, not generation unity.* Native palette and restraint beat
   grading: the failed composites carried synthetic light events (pools, halos, dusk
   overlays) and per-layer grades; the working scene deleted all of them and let the sky
   plate's own ember through the stalk gaps. Add light only where the scene claims a
   source.
6. *Diegetic text takes its environment's material properties.* Text living inside the
   scene (painted on the sign) is baked as an art layer: glyph color MODULATED by the
   underlying surface luminance (paint follows the planks) + alpha ERODED by a wear map
   (real flake holes, not a multiply overlay) + scene occlusion. Occluders clip STROKES,
   never letterforms, and never a word's first letter (readers reconstruct middles, not
   heads). Baked text forfeits the composer's contrast gate — check it manually.
   Non-diegetic text (author line, eyebrow) stays composer-gated, and deliberate
   text-behind-corn depth must be baked too: the occlusion autopilot reorders any
   TextSlot whose ink an art layer touches past its limit.

7. *Contained-by: a container's opening is measured, never derived.* (The Badgerbones
   addendum — the third real cover, 2026-08-30.) An element living inside a container
   (an ornate picture frame, a window, a porthole) is a fifth physical relationship
   beyond standing-on / painted-on / attached-to / floating, and it shipped clipped
   TWICE before anyone measured anything: the cone's scoop hid behind the frame's top
   rail in two delivered versions, because the element was placed by arithmetic on the
   container's bounding box — and an ornate container's opening is NOT derivable from
   its bbox (crests and scrollwork intrude far past the rail line). The opening is
   found by flooding the container's alpha from the interior (`inspect.opening_bbox`);
   the gate is `inspect.containment_check`: the contained element's hard-ink bbox
   (alpha > 40) must sit fully inside the measured opening with ≥1%-of-canvas margin,
   computed over compose's OWN placement of both layers, never a re-derivation. A
   deliberate breakout (the object-breaking-the-frame move) stays legal but must be
   declared in the spec's rationale — a crossing nobody declared is a FAIL, same class
   as `dead_band > 0.2`. Corollary process rule, learned the hard way: never assert a
   contact property ("seated", "clear of the rails", "inside") without having run its
   probe — a claim with no probe behind it is the tell that a relationship is missing
   its gate. And thumbnails cannot gate contact: a ~40px rail clip is invisible at
   300px; contact regions get zoomed rail/ground crops on every delivered version.

8. *Depth bands must differ in VALUE, not only in order.* (The Tabletop Emperors
   addendum — the fourth real cover, 2026-08-31: a neon motel sign at dusk, sky / ridge
   / roofline / sign.) Two silhouettes generated for different depth bands came back at
   effectively the same ink — ridge `15,11,35`, motel roofline `11,10,26`. Composited in
   the correct order they read as ONE mass, and the owner reported the far band as
   covering the near one; the z-order was right and completely invisible. Depth is
   carried by aerial perspective, so blend the FAR band toward the sky behind it (0.42
   here) and leave the near band black. Ask for the separation in the prompts as well,
   but never rely on separately generated silhouettes arriving at usable values relative
   to each other — measure the mean ink of every adjacent pair and fix it at composite
   time. Note what this failure looked like from inside: correct layer order let the
   whole question of depth stop being thought about.
9. *Applied lettering is a set of OBJECTS, not a typeface.* Extends rule 6. Where
   diegetic text is physically applied — changeable marquee letters in a rail, pin-on
   store lettering, stick-on vinyl, pegboard — drawing the string with a tracked font
   reads as printing no matter how good the font is, because every tell of the real
   thing is an irregularity BETWEEN letters. Render one tile per glyph: rotation
   ±1.5–2°, baseline jitter, size variance 97–103% (a real board mixes sets), per-letter
   ink value, uneven advance, wide word spaces. Then treat the tiles as objects standing
   off their surface — contact shadow, a lit top bevel, and for a BACKLIT board the
   decisive cue: a bright fringe just OUTSIDE each glyph where the panel's light wraps
   the tile edge. That fringe is what says "object in front of a lamp" rather than "ink
   on a surface". Wear maps stay light — eroding tile alpha toward ~50% shows the lit
   panel through the letter and the set reads faded and printed, the opposite of worn
   plastic. General form: an effect written for one material does not survive a change
   of material; re-derive it from what the new material does to light.
10. *Fitting type into a measured opening that is not a rectangle.* Corollary to rule 7.
    Once the opening is measured, the largest inscribed AXIS-ALIGNED rectangle is the
    wrong box for any non-rectangular container: inside an acute boomerang wedge it
    shrank a neon script to 68% of what the shape actually holds, and the containment
    gate passed while the design failed. For any opening that is convex per row — wedges,
    parallelograms, chevrons, most sign panels — take each mask row as an interval
    [lo, hi]; for every vertical offset solve the feasible horizontal offsets in closed
    form against the glyph run's per-row ink extents, and sweep font sizes keeping the
    largest that has a feasible placement. Exact containment, ~3× the type. Inset the
    opening by the stroke's own radius first (a neon tube must not touch the chrome).

**A gate proves legality, not quality.** Every defect that survived a version of the
Tabletop Emperors cover was one no gate covered, and the worst of them shipped inside a
render whose gates all printed OK — `containment: 0 px outside → OK` sat directly above a
title set at 68% of the size its panel would hold. Containment, alpha and layer order are
cheap to compute, which is exactly why they get mistaken for judgments they cannot make.
The rule that follows, and it generalizes past covers: look at the render before making
any claim about it, and use the numbers only for the questions they actually answer. Two
claims in that job were asserted from the code without looking — including "you can see it
working in the render", about a render showing the opposite.

**Gates this adds** (beyond §15.19's): zero visible ground contacts in mode-two scenes
(extend contact_gaps: a termination against sky/cloud/canopy = FAIL); ink-outside-surface
== 0 for every painted-on decal (mask by surface minus off-plane hardware, correct by
construction); the 100px thumbnail read on every delivered version, not the last one;
containment_check's min gap ≥ margin for every declared contained-by pair, with
undeclared opening crossings a FAIL (rule 7); adjacent depth bands separated by ≥0.10 in
mean relative luminance across the rows where they meet, unless a merged silhouette is
declared deliberately (rule 8); and, whenever a layer is scaled past the canvas, an
enumeration of the named features leaving the frame — the motel's TV aerial was cropped
away by a scale chosen for the roof band, and only the owner noticed it was gone.

**Pixel traps (new this cover)**

- Corner-median background sampling fails when two corners are ink — take the brightest
  corner as ground truth.
- Recoloring bright pockets must touch only fully-opaque pixels (alpha > 230); touching
  anti-aliased edges paints halos around every stalk.
- "Transparent" gen output may carry garbage RGB in its alpha-0 regions — it composites
  fine but previews as smoke; judge assets over a neutral ground, never by raw preview.
- Enclosed white pockets never reach a border flood; under native-alpha assets the whole
  flood-fill pipeline is legacy — request `background: transparent` and floor the sub-40
  haze band instead.
- A screen-blended texture is a void-black tool ONLY: on a warm ground it adds white
  light and greys the saturation right out (the damask sheen that fixed a black wall
  visibly deadened an oxblood one). Enrich a warm ground in the plate itself —
  saturation/contrast plus a soft never-void vignette baked into the crop — not with a
  lightening layer.
- Zooming into a plate by cropping discards the crop's contrast range along with the
  edges; re-enrich after the crop or the cover reads washed out at full bleed.
- A generated plate ends in a hard straight cut. Wherever the layer in FRONT of it has a
  gap, that cut shows as sky under the scene and the band reads as floating — run the
  plate's own ground row down to the canvas edge (replicating one flat row is an
  extension, not an invention).
- Patching a hole in a flat silhouette by copying each column's edge pixel smears whatever
  that edge happens to be — a palm trunk, a rust highlight — into vertical streaks. Fill
  with the silhouette's median body colour sampled from deep inside it.
- A large lit panel (a reader board, a lightbox) is bright enough to trip a bulb-halation
  luminance threshold and will bloom like a bulb. Attenuate that region specifically;
  lowering the global halation stack costs the real bulbs their glow.
- Fitting type untracked and then drawing it tracked overruns its container — here by
  ~90px a side on the reader board. Fit what you actually draw.
- Pillow 12's `ImageDraw.floodfill` silently fills nothing on an "L" image: it returns
  normally and the mask comes back empty, so the failure surfaces several steps later as
  a confusing error on a zero-size array. Use a scanline flood, and assert non-empty at
  the point of measurement.

### 15.21 Clipped-art doctrine — the value contract for masked slots (the Drop Table addendum)

Provenance: Drop Table (2026-08-31) — a `title_window` concept where
the artwork is visible ONLY through the title's own letterforms. It failed on the first
render for a reason no amount of art direction would have fixed, and passed on the second
for a reason that generalizes to every masked slot in the library.

**Read this section the way it was learned, not as a checklist.** §15.19 and §15.20 govern
scenes; this one governs a narrower thing — art that is CLIPPED before it composites
(`title_window`'s `window_art`, anything wearing `mask: {from_text: …}`, a `split_plate`
band). Exactly ONE item below is a contract, because the composer measures it and will
act on its own if you get it wrong. Everything after it is a default: where a cover has
a better idea, the cover wins. A doctrine that makes the next twenty covers look like
this one has failed at its job.

**The contract (measurable, enforced by the composer, not negotiable by taste)**

Clipped art and the field it sits in must be OPPOSITE in value, and the art's own value
must be uniform edge to edge.

The mechanism is §7.4a plus §15.7. `art_fill` text has no ink of its own — nothing is
painted inside the glyphs, the clipped art simply shows — so the legibility autopilot has
no ink color to test and falls back to `palette.primary`, the thin ring at each glyph's
edge, measured against whatever dominates the title's zone. Get the direction backwards
and the autopilot does exactly what it was built to do: escalate the scrim that the
archetype ships at strength 0.0, and paint a panel behind the title. On a `title_window`
cover that panel IS the design, destroyed to protect itself.

Measured, same brief, same art, only the value direction swapped:

| | field | clipped art | title contrast | scrim_final |
|---|---|---|---|---|
| fail | near-black `#15130F` | pale aged paper | 1.00 → ink-flip 3.26 → **3.33** vs 4.5 | **0.85** |
| pass | cream `#DDD2B8` | microfilm negative | **7.22** | **0.0** |

The autopilot's own escalation ladder is the tell: an `art_fill` archetype that finishes
with a nonzero `scrim_final` has not been rescued, it has been overruled. Treat that
number as a build failure and re-direct the value, not the scrim.

**Defaults — starting points, each one overrulable by the cover in front of you**

1. *Prompt for a texture, not a picture.* You cannot predict which fraction of the frame
   lands inside a glyph, so anything behaving like a composition — a subject, a margin, a
   bright corner — is wasted where it is clipped away and ruinous where it isn't. The
   negative constraints carry more weight than the style adjectives: edge to edge, no
   margin, no border, no empty region, no single focal point, no words. `title_window`'s
   `composition_note` says this to the image model; say it again in the art prompt.
2. *When the real object is the wrong value, change the reproduction medium — not the
   palette.* Drop Table needed a paper document to read DARK. Asking for a dark document
   fights physics; asking for a **microfilm negative** — pale figures on black film —
   delivers a dark, uniform, evenly dense frame that is also period-true for a records
   office. Carbon copy, photostat, blueprint, X-ray, and press negative are the same move
   at other values. Reach for the medium before you reach for a grade: this is §15.20's
   rule 5 (coherence is native, not graded) arriving at a different door.
3. *Iterate on a $0 procedural stand-in before generating anything.* Draw a fake of the
   clipped art with PIL — density and value are all that matter, so a crude one is
   enough — and lock archetype, palette, fonts, and zones against it. Both Drop Table
   failures (the value contract, a 28% dead band) surfaced against the stand-in, for
   free, before a single generation. The real asset then drops in with no layout work
   left.
4. *"Texture on the background" is a slot, not a recipe.* Paper tooth under the
   letterforms is its own `ArtSlot` inserted directly above `background`
   (`texture_file: laid_paper`, `texture_fit: tile`, ~0.20, multiply) so it grains the
   field and leaves the clipped art clean. A finishing recipe's own paper layer
   (`vintage_matte`'s `fx_paper`) sits above the WHOLE stack, clipped art included, and
   is doing a different job. Both can be true on one cover.
5. *Judge grain at 1:1.* A downscaled comparison strip made four different texture plates
   look identical; native-resolution crops immediately showed `canvas_weave` pushing the
   ground grey-green — bookcloth, not paper. Never pick a finish from a thumbnail. (The
   100px thumbnail read from §15.20 is a different test asking a different question; run
   both.)
6. *Re-read the RenderReport after every text change, including the trivial ones.* Stock
   archetype zones assume longer strings. A two-word `justify_stack` title left a 28% dead
   band at the top; swapping a long subtitle for a short one then unbalanced the gaps
   (17.0% vs 13.6%) even though nothing else moved. `warnings` and `dead_band_frac` are
   free design QA and both fixes were a zone nudge.

**Gates this adds** (beyond §15.19's and §15.20's): on any archetype whose title runs
`mode: art_fill` or `knockout`, `scrim_final` for the title must be 0.0 on the delivered
version — a nonzero value is a failed value direction, not a protected title; and the
clipped art's own frame must show no region at the field's value (sample it before
compositing, not after).

**Traps (new this cover)**

- A generated document will invent its own data. Drop Table's table starts at 100 lbs
  when the story's whole hinge is that it stops at 110 — legible numerals inside
  letterforms are *read*, so brief the content, not just the look.
- The stand-in is not the asset. A procedural stand-in tuned to pass the contrast gate can
  flatter a generated asset that will not; re-read the report after the real art lands.
- Masked slots inherit their archetype's mask, never a direction's `mask_intent` — an
  intent named on a slot the template already masks is dropped with a log line, so a
  concept that needs a different clip needs a different archetype.

### 15.22 Second-pass doctrine — the seven things that stood between a plate set and a cover (the Willow On Me II addendum)

Provenance: the second Willow On Me cover (2026-08-31) — the same book §15.19 came from,
but run the way the product is supposed to run: the author generated four plates from
prompts the engine wrote, and everything after that was ours. Twenty-eight renders, five
delivered versions, and every one of the author's notes traced back to a step the engine
does not yet own. This section is that list. It is written for the `manuscript in → 4-6
covers out` target: each item is a thing the model has to KNOW, and most of them are a
thing the model has to MEASURE rather than choose.

**The governing lesson, in the author's words: "a decision is made, and then everything
must flow downward from that. It is not so. The scene is malleable until it's sent."**
And its operative half: **if something isn't working, change it up. See what does.**

This is a general rule about how to iterate, not a note about any one element. The failure
mode it names is refining a failing approach instead of replacing it — and it happened in
four different places on this cover:

- *The airship.* The plate crop was treated as terrain, so the ship got squeezed into
  whatever void was left; when it was too small to carry the author line, the fix reached
  for was a smaller author line. The actual fix was a different location and twice the
  size — which resolved the dead band, the legibility, and the cover's missing second beat
  at once.
- *The mark.* Three rounds went into tuning a grammar the author had already rejected —
  thinner strokes, softer flashes, better spacing — when what was needed was a different
  grammar (three narrative anchors), not a better-tuned version of the wrong one.
- *The figure.* The second plate was better rendered, so it looked like an upgrade; the
  author swapped back to the first for its story of movement. Better execution of the
  wrong choice is not progress.
- *Diegetic type.* Nudging a pasted-on caption's position and opacity never made it belong.
  Deriving its geometry from the host did.

**Operationally: when a fix does not move the read, change the KIND of thing you are doing
before you change its parameters again.** Two failed refinements of the same approach is
the signal to swap approaches. For the planner this means the search space is the plate
transform, the figure's scale and position, every element's zone, and the archetype itself
— not the element being complained about. Encode elements as constraints ("the author line
reads at 180px", "no dead band over 0.28") and let it move whatever satisfies them; a
planner that can only adjust the thing under discussion will converge on a worse cover than
one that can re-frame.

---

**1. Plates do not arrive in the aspect you asked for. Landscape rescue is a first-class
step, not a repair.**

The prompt said 2:3 portrait; the background came back 1402×1122 landscape. This is not
the author's error and it will recur — treat portrait as something the engine *builds*.
The op has four parameters and one rule:

- `zoom` — scale past canvas width so the city fills more vertical space (1.72 here);
- `cut_bias` — **which side the overflow comes off, chosen by which landmarks must
  survive.** 79% came off the left specifically to keep the airship and the cliff edge,
  the two details that prove the island floats. Never centre-crop; ask the brief which
  three landmarks are load-bearing and protect them.
- row-wise sky extension — per-column extrapolation of the plate's own top band toward a
  ceiling colour, eased;
- seam crossfade — a real blend over ~90 rows between the extrapolation and the plate.

Two defects cost a version each and both are now rules. **Smooth the seam row horizontally
(σ≈60) before extrapolating from it**, or you extrapolate its grain into vertical banding —
and per §15.19, a step survives a blur, so this is unfixable downstream. And a "feather"
that is not an actual crossfade leaves a bright hairline at the seam; write the blend,
don't gesture at it.

**2. Every anchor is measured. Estimating off a downscaled preview has ~10% error, which
is fatal at flash scale.**

I placed the bell-tower vertex by eye and missed by 86px, so the arrival flash floated in
the sky beside the tower instead of landing on its cross. §15.18's inspection kit exists;
this cover is the argument that it is not optional. Before choosing any vertex, seat, or
type zone, render a **ruled-grid crop at known scale** over the region and read the number,
or detect the feature programmatically. "Display coordinate × scale factor" is not a
measurement.

**3. Occlusion-aware placement — the figure eats the scene.**

At 42% of cover height the figure covered x∈[150,1114] of a 1600px canvas: most of the
"city" the mark was supposed to travel through was behind her. Two iterations were spent
routing a mark through pixels she covers. The engine needs a **free-interval map** —
per row, the column spans not covered by the seated figure's alpha — and every vertex that
must be *seen* has to sit in one. Vertices that are deliberately hidden (a terminus behind
the subject) are fine and often correct, but they must be chosen as hidden, not discovered
to be.

**4. Grounding a cutout is a PLANNED step, and it is the whole stack — shadow and rim
together. Think before prompting.**

The model should have known, at the moment it wrote the figure prompt, that a transparent
cutout dropped onto a rooftop needs to be grounded, and should have said so out loud:
*she'll need a drop shadow under her.* The prompt correctly asked for "no ground, no
shadow, no scene" — a cutout must arrive clean — but the plan that prompt belongs to must
already carry the grounding work as required, with its own gate, so it is executed before
the author ever sees the render. Instead the whole stack was built reactively, after the
author said she looked like she was floating. **Every plate the brief requests implies
integration work; enumerate that work when you write the prompt, not when someone
complains.**

Both halves of the stack were load-bearing and neither is optional:

  a. **The shadow work is essential and comes first in the plan.** A projected cast shadow
     from a real affine derived from the light's elevation, with a severe vertical squash
     (m ≈ 0.15) — a mild squash puts the shadow up on the buildings instead of the ground;
     the first attempt landed at y=1451 on a plane that starts at 2150. Plus a tight
     ambient-occlusion pool (radius ≈ 40, not 58) and a penumbra **raked away from the
     key**, never symmetric about the contact. And **lift the receiving surface first** —
     a cast shadow on near-black tiles is invisible, so rake the horizon key across the
     plane before darkening any of it.
  b. **The contact itself is a hard, unblurred weld** along the silhouette's bottom edge —
     2–3px, blur ≤ 2. Every softer version reads as a cushion the figure is resting ON.
     This is §15.19's "contact-POINT shadows, never a body-wide bar" one level finer: not
     just small, but *hard*.
  c. **And the rim light must die before it reaches the ground.** This was the piece that
     had been missing while three rounds of shadow work went in, and it is worth calling
     out precisely because it is the one nobody looks for: the synthesized key rim was
     running all the way around the silhouette, *including under the boot sole and the
     fingertips*. A lit edge where a thing touches the ground is the visual signature of
     hovering, and it will outvote correct shadows. Kill the rim in the bottom ~4.5% of
     the figure and darken those rows into their own shadow.
  d. **Do not "reseat"** (paste the surface back over the figure's lowest rows) once a hard
     weld exists — it re-lit a bright tile edge under the sole and un-did (b).

The debugging order to remember: if the shadows are right and the figure still floats,
check the rim at the contact. If the rim is right and it still floats, check that the
surface is light enough for a shadow to exist on.

**5. The signature mark's anchor count is a NARRATIVE number, not a density knob.**

Three grammars were tried and the author rejected two of them:

| version | grammar | verdict |
|---|---|---|
| one continuous stroke | "reads as a line" (§15.19 rule 3, confirmed) | rejected |
| dashed connectors + graded flashes at 5–6 vertices | reads as hops, but "doesn't need balls in the middle" | rejected |
| **three anchors, bold continuous strokes, flashes only at anchors** | accepted | **ship** |

The rule that generalizes: **origin, one discrete bounce, terminus — and every anchor must
be a nameable object in the scene** ("the minaret's spire", "the bell tower's cross"), not
a point in the air. The origin is a *story* choice, not a compositional one: "from the sky"
and "from a moored airship out in the Depths" were both rejected in favour of "from the
city", because that is where the character came from. The brief must extract the mark's
origin along with the mark.

Two mechanics fall out of the three-anchor form. **Anchors at similar heights make the
outbound and return legs retrace one corridor** and merge into a single fat blob — separate
the terminus vertically (here, down onto the pack). And on a light ground the **flash needs
the same inversion as the stroke** — §15.19's glow-inversion trap applies to the burst, not
only the line, or it reads as sun glare. Light-ground recipe: dilate+blur the mask into a
dark underprint (×0.78), then an opaque saturated body that *includes the flash term*; no
bright core, no additive spill.

**6. Diegetic type is a measurement problem, and it decomposes cleanly.**

"It shouldn't feel pasted on" and then "the ship must act as a FRAME — neat and centred
INSIDE the hull; if you need to bend the text, bend the text" are the two halves of one
build. Nothing here is a taste call:

  a. **Take the geometry from the host.** Erode the host's alpha until its fixtures
     (rigging, fins, gondola) fall away, then read the surviving body **column by column**:
     centre line and half-height. Bend the baseline along the centre line; taper the
     letters by the half-height.
  b. **The window must be SYMMETRIC.** A blunt nose and a tapering tail make the naive
     "where is it fat enough" window lopsided, and a lopsided window reads *crooked*, not
     curved — the name sat level on the left and dove into the taper on the right. Take the
     widest symmetric window about the fattest column.
  c. **Damp the conformance.** Centre-line deviation ×0.78, taper clamped to [0.80, 1.18].
     Following the hull exactly caricatures it.
  d. **Modulate the ink by the host's high-frequency DETAIL — `lum − blur(lum)` — not by
     its absolute luminance.** Modulating by darkness is the obvious move and it produced
     near-illegible ghost lettering, because most of that hull is dark. Detail is what
     makes the ribs and panel seams cut through the strokes while the letters stay bright.
     Add the broad shading at low weight (0.74 + 0.30·shade) and a seeded wear field.
  e. **Clip the ink to the host's hard alpha** so it cannot spill past a taper.
  f. **Fit both axes**: the string's width to the window's width AND the em box to its
     height, shrinking the face until both hold.

The payoff is not only that it looks painted. A diegetic author line sized to a real object
is *large* — this one is ~56% of the hull — which is what finally made it legible at
thumbnail after the small pasted version failed that test.

**7. Call the engine's metrics. Never re-implement them.**

A hand-rolled cell-variance dead-band proxy reported 0.44 and 0.63 (FAIL) on covers whose
actual `compose._dead_band_frac` — the tallest run of near-flat **rows** — was 0.105 (PASS).
Two numbers, one of which is the shipping gate. A reviewing agent that invents a metric
will re-litigate decisions the composer already settled. Related: WCAG contrast read off
the *flattened* composite is pessimistic, because the cream type is inside its own
measurement zone; measure the backdrop pre-type or accept the number as a floor.

---

**Plate briefing, revised (supersedes the ad-hoc prompts §15.19 implies)**

What worked verbatim and should be locked into the prompt library: the **material plate**
("full-frame abstract texture, pure black ground, uniform density edge to edge, no margin,
no border, no empty region, no single focal point, no subject") produced a perfect
luminance-mask material on the first generation. The shared **style block + palette hexes +
lighting contract** made four independently generated plates cohere. Ask the image model for
**materials and subjects only** — the geometry stays Pillow's, per §15.19.

Two additions. **Ask for 2–3 figure poses, and select on narrative, not render quality** —
the author supplied a second, better-crafted figure mid-session and then chose the first
one back because it "has a better story of movement" (streaming hair and a reversed dagger
imply a before and an after; a static crouch does not). And **audit every plate on arrival**:
aspect vs requested, alpha haze fraction, hard-vs-raw bbox delta, and — for a material —
whether it is actually uniform. Both figure plates and the airship carried a glow baked into
RGB behind clean alpha; the airship needed hard-keying at a threshold or its bbox haze
floats the object.

**Gates this adds** (beyond §15.19–15.21)

- *floating figure*, checked as a stack, all four required: a cast shadow exists on the
  receiving plane; the plane is light enough for it to register; the contact carries a hard
  weld; and no rim-light contribution survives in the bottom 5% of the seated figure's
  alpha. Any one missing is a fail — they do not substitute for each other.
- *unplanned integration*: a plan that requests a transparent cutout without also naming its
  grounding work is a fail at PLAN time, before a single plate is generated.
- *mark reads as a line*: a signature mark with fewer than two segments, or with vertices
  that are not on detected scene features, is a fail.
- *type reads as pasted*: diegetic lettering whose ink does not vary with the host's local
  detail is a fail; so is lettering whose window is asymmetric about the host's widest point.
- *invented metrics*: a review that reports a gate number not produced by `compose` is void.

**Build list (each item PR-sized, and each one is a note the author should never have had
to give)**

1. `plate.py` — landscape rescue as a parameterized op: zoom, landmark-preserving crop bias,
   smoothed-row sky extension, real seam crossfade.
2. `inspect.py` — ruled-grid probe and `free_intervals(figure_alpha, pos)`; make anchor
   selection consume them.
3. `integrate.py::seat_figure` — rim-kill at contact, hard weld, raked penumbra, tight AO,
   projected cast shadow from light elevation, surface lift before darkening.
4. `integrate.py::mark_painter` — three-anchor default, anchors snapped to detected features,
   luminance-aware inversion applied to strokes *and* flashes.
5. `integrate.py::diegetic_type` — host-body measurement, symmetric window, damped bend and
   taper, detail-modulated ink, both-axis fit. The brief must name the diegetic surface the
   manuscript supplies (this book hands you an airship hull).
6. Brief extraction — add: the mark's story ORIGIN, the pose's implied motion, the three
   load-bearing landmarks, and a diegetic surface for the author line.
7. Planner — constraints over positions: let it re-frame the plate, re-scale any element,
   and switch archetype to satisfy a gate, per the governing lesson above. Track how many
   times a given approach has been refined without moving the read; at two, force a
   different KIND of move rather than another parameter pass.
8. Plan-time integration manifest — every requested plate carries the list of integration
   steps it will need (a cutout implies grounding; a material implies a mask target; a
   scene implies a landmark budget), so the work is scheduled with the prompt.

**Determinism held, and it is what made twenty-eight versions affordable.** Every render
rebuilt byte-identically from a seeded script plus the four plates, verified at each
delivery. §15.19's promise — "every cover an archived spec that re-renders byte-identically
and revises for $0" — is the reason a five-round conversation cost plate generation once.

### 15.23 Standing figures need a ground — generate one (the Desaturated II addendum)

Provenance: the two *Desaturated* covers (2026-08-31). Every gate in §15.19–15.22 printed OK on a
delivered cover whose figure was standing on nothing, and the owner named it in four words on
first sight. It is the loudest defect on the list and it had no rule, so it gets one.

**THE CARDINAL RULE. If a figure is standing, they MUST look like they are standing on
SOMETHING.** Not "should", not "prefer". This outranks every other note in this section, and it is
the one thing a reader adjudicates instantly and without vocabulary. A figure that floats has no
redeeming composition: the palette, the type and the mark are all wasted behind it.

**And the fix is usually a new plate, not better shadow work.** This is the half that was missed.
The scene plate here was an elevated view looking DOWN across rooftops — a good plate, exactly as
briefed — and it contains **no floor plane anywhere in the frame**. There was nothing to seat her
on. The instinct at that point is to hunt the plate for the nearest horizontal-ish surface and
wedge the figure onto it; that is the ham-fisted move and it never survives review. §15.20's
third-variable rule applies and names the answer: when two constraints cannot be reconciled by
moving the element, reach for a variable neither of them names. Here that variable is **a ground**,
and a ground is one more generation.

  *When a character is floating, you do not cram them onto some other platform. You generate them
  their own space.*

The near band cost one image (~$0.05): a roof deck at standing eye height, running away to a low
parapet, no sky and no city in the frame. It becomes its own depth band (§15.20 rule 4), the
plate's own parapet becomes the boundary with the far band — a real object edge instead of a
straight cut — and the figure then has something to be seated on with all of §15.22 item 4's
machinery.

**Plan-time consequence.** §15.22's *unplanned integration* gate already fails a plan that requests
a cutout without naming its grounding work. Extend it: a plan that requests a **standing** cutout
must also name **the surface it stands on and which plate that surface comes from**. "The
background plate" is only an acceptable answer if the background brief actually asks for a floor
at the figure's feet, in the figure's perspective. If it does not, the plan is short one plate and
is a fail before anything is generated.

**Gate.** *No ground under a standing figure* — refuse to deliver when the plate the figure is
seated on has no surface at the contact. Checked as a stack, all of it required (this is §15.22's
floating-figure gate, restated with its missing first term):

0. **a receiving surface exists in the scene at the figure's feet, in the figure's perspective;**
1. a cast shadow exists on it, thrown by a real affine from the key's elevation;
2. the plane is light enough for that shadow to register — lift it BEFORE darkening it;
3. the contact carries a hard, unblurred weld;
4. no rim-light contribution survives in the bottom 5% of the seated alpha.

Term 0 is new and it is the one that cannot be fixed downstream. Measure the result rather than
looking: sample mean plane luminance in rings out from the contact and require a monotonic
gradient into it (this cover: 83.6 unshadowed → 81.5 → 67.5 at the contact).

**Pixel traps (new this cover)**

- `fit: "cover"` fills the whole canvas — a *band* cannot be made with `scale` and `offset`. The
  deck did exactly that on the first attempt and swallowed the city, the moon and the flier. Build
  the band as a full-canvas RGBA whose alpha is cut at the band's top row.
- A cast-shadow affine has a sign, and both signs render. With the key low and BEHIND the subject
  the shadow comes toward the camera: `out_y = cy + m·(cy − in_y)`. The other sign throws it up
  behind her onto nothing, which looks like no shadow at all rather than like a bug.
- `treatment: "silhouette"` fills with **`palette.primary`**. If primary is the title's ink, the
  figure renders in the title's colour and craters its contrast — measured here at 3.61 on a
  cover whose title had read 5.73 at draw time.
- A whole-word occlusion budget cannot see a destroyed letter. One glyph of eleven is ~9% of the
  ink, so "title occlusion ≤ 8%" passed on a cover reading `DESATU_ATED`. The rule was always
  "occluders clip STROKES, never letterforms": measure per letter, and note that the type's own
  drop shadows bleed across every inter-letter gap, so strip effects before segmenting or the
  per-glyph gate silently degrades into the whole-word one.
- A limb probe needs a width **floor** as well as a ceiling, and must scan the figure's own bbox
  rather than the canvas: without the floor it tracks a gap in the ponytail, without the ceiling it
  runs past the hand into the two legs (also two separated runs) and returns a torso-width band.
  Three versions of this probe shipped a visibly wrong bracelet before it was constrained.
- `inspect.isolate()` returns a **flattened composite** — every pixel alpha 255 — so it cannot be
  used to read a layer's shape. `placed_ink_mask` is the tool, but re-ink the slot white first when
  the cutout is near-black on a near-black plate (its own docstring names that failure mode).
- Generated cutouts arrive with 140–240px of sub-40 alpha haze, and `contain`-fit scales the whole
  source frame — so that padding silently shrinks every object. Crop to the hard-alpha box during
  conditioning, not at seat time.


## 16. The director and the atelier (DECIDED 2026-08-31, owner) — how a cover is made now

The old flow was: distil a manuscript *sample* into a grounding sheet, ask one call for N directions, then paint each concept and put it through a fixed critique loop. The new flow is two acts, and it is what the owner arrived at by building six Longsword covers by hand with six agents:

> **One model reads the book and assigns cover specs, and N agents execute the plan, iterate, and return when finished.**

### 16.1 The director (`docproof/cover/director.py`)

ONE structured call on the frontier model (`DIRECTOR_MODEL`, `claude-fable-5`), given the **whole manuscript**, answering with:

- `reading` — what this book is actually about in the terms a cover has to work in: the concrete objects and places the prose returns to, the register, and the single thing a browser must understand in one second.
- one `ConceptAssignment` per concept: a `Direction` (the design — archetype, palette, fonts, art prompts, recipe, type move, exactly as §6.1 defined it) **plus two pieces of prose a schema alone cannot carry**:
  - `execution_notes` — the specific way THIS design fails if built carelessly, and the clause the art prompt must carry to pre-empt it.
  - `done_when` — the finish line, concrete enough that the agent can judge itself against it at thumbnail size.

Those two fields are the whole reason the director exists as a separate thing from the old direction call. The agent that executes a concept has not read the book; everything it knows about why this cover is right comes from here.

`fit_manuscript` is the one concession to physics: at or under `MAX_BOOK_WORDS` (120k) the text goes in whole and unlabelled; past it, the book is read as `BOOK_SLICES` (8) labelled slices spanning its whole length, with the opening and the ending at double weight. **Sliced, never truncated** — a cover designed off the first 120k words of a 300k-word novel is designed off the setup, and the book's own answer never reaches the director. `DirectorResult.sliced` and `.words_read` are ledgered, so nobody believes a long novel was read end to end when it was not.

Failure posture is `run_directions`': `DirectorError` on any trouble, no fallback, because the next step spends real image-generation dollars.

### 16.2 The atelier (`docproof/cover/atelier.py`)

One agent per concept — a Claude Code session over the agent SDK (`docproof/agent_lane.py`), on the owner's subscription — holding the composer's own verbs as tools:

| tool | cost | what it does |
|---|---|---|
| `read_spec` | free | the current CoverSpec as JSON |
| `archetype_info` | free | this archetype's slot ids, which are generatable, which carry an authored mask, the text zones |
| `budget` | free | generations and dollars left |
| `paint` | **money** | generate one slot's art at `1K` or `2K` and attach it |
| `render` | free | compose and return the render report |
| `look` | free | the composed cover, downscaled to about thumbnail size |
| `edit_spec` | free | patch the spec by JSON path and recompose — the same vocabulary and the same guarded paths `revise_spec` uses |
| `finish` | free | declare the cover done, with an honest summary |

Four things this is built around:

1. **The tools are the studio's own verbs.** `paint` runs the pipeline's own prompt assembler, so the archetype's composition note and the negative suffix cannot be bypassed by an agent writing its own prompt. There is no second rendering path to drift onto.
2. **Money is metered in code, not trusted to the agent.** `Budget` is checked before every generation and the refusal comes back as the tool result — a sentence the agent can act on. Both ceilings bind: `MAX_GENERATIONS` (12) stops an agent that has lost the thread on cheap rolls; `MAX_ART_USD` stops one that escalated everything to 2K. The agent MAY choose the tier — escalating a keeper to 2K is exactly the judgment worth delegating — and every roll is priced from the same tier it rolled at. `pipeline._concept_art_budget` scales the dollar ceiling with the job's tier, so a draft job buys the same NUMBER of rolls as a full one.
3. **Finishing is a decision, not a timeout.** An agent that stops calling tools without `finish` has run out of turns, and that is recorded as a different outcome from a cover its builder called done. Either way the composed cover ships — throwing away art the person paid for would be worse — with the fact in the ledger.
4. **A failed concept is one concept.** `build_concept` never raises for anything that is one concept's problem; it returns a `ConceptOutcome` carrying `error` and whatever was composed. The two exceptions are the lane's own refusals (no SDK, no login), which are the whole job's problem and are raised.

### 16.3 Concurrency — N agents at once (reverses the 2026-08-31 serial rule)

Concepts are built **concurrently**, `asyncio.gather` over `_build_concept`. This reverses "one concept at a time," decided earlier the same day, and the reason it is safe is that the thing serialisation protected no longer exists: concepts used to interleave through one image semaphore while a shared judge loop and staged reviews waited on each other's generations. An atelier session is its own reasoning process holding its own budget, and the per-job image semaphore still bounds what is actually in flight. N agents therefore cost about as long as one — which is what the owner watched happen building the six Longsword covers.

### 16.4 What this orphaned

- **`critique.py` — deleted.** Its loop is §16.2.
- **`reality.py` — deleted.** Its sample distillation is the director's whole-book read. `Providers.reality` remains as an accepted keyword so existing callers keep working; nothing reads it.
- **The §15.16 composition planner no longer runs on a new job.** It was the pre-flight that made several independent generations into one planned composition; an agent that reads the archetype, argues itself into a prompt and looks at what came back is doing that job with its eyes open. `planner.py` stays for `run_revision`'s replan path, and `COVER_PLANNER` now gates only that.
- **The `critique` doctrine surface is now `atelier`**, and it inherits `canvas`'s conduct rules ("measure before you move", "fewest ops") because, like the canvas assistant, the atelier is a tool-using conversation that can obey them.

### 16.5 Deployment consequence — READ THIS

The agent lane needs the Claude Agent SDK **and a Claude login**. `Dockerfile.quest` now installs `.[app,cover,canvas]` for the SDK, but a Fly machine has no interactive login: it needs `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` in its secrets, or **every new cover job will fail at the first agent** with the lane's own sentence. There is deliberately no fallback path — the owner chose replacement over a dual-mode engine (2026-08-31).


## 17. Three measurement bugs (FIXED 2026-08-31, from the Longsword run)

Six covers built by six agents surfaced three defects in the composer that share one shape: nothing crashed, nothing warned, and each returned a number that looked reasonable while answering a subtly different question from the one its caller had asked. All three are the kind an agent hits immediately, because an agent reads the render report and believes it.

### 17.1 The duotone tone crush

`_duotone` (and `_posterize`) positioned each pixel on its ramp by **WCAG relative luminance**, reusing `effects.luminance_band` on the reasoning that one measurement serving two purposes was a virtue.

Linear luminance is savagely compressed at the dark end. A plate living between sRGB 10 and 60 — a night interior, a dark table, deep water, i.e. exactly the plates a duotone is reached for — spans linear luminance 1 to 17 out of 255: a ~6%-wide slice of a 256-step ramp. Every tone in the plate collapsed onto effectively one colour and the treatment returned a flat rectangle. The agent building "The Empty Seat" hit this, measured `|ΔL| = 0.009` against a 0.12 floor, and worked around it with a `gradient_map` adjust layer.

**Fix:** new `effects.lightness_band` — the same WCAG channel weights applied to the **gamma-encoded** values, with no trip through linear space — and `_duotone`/`_posterize` read it instead. For a neutral pixel it is the identity, so every tone the plate had survives (64 test tones in, 64 out; it was 5).

Note the rejected first attempt, which is the instructive part: `luminance_band(...).point(srgb_encode)` looks equivalent and is worse, because `luminance_band` quantises to 8 bits *while still in linear space* — the dark detail is already destroyed before any re-encode can spread it back out (64 tones came back as 17).

**`luminance_band` is unchanged and still what the legibility autopilot uses.** Contrast math is the one place linear is right. The lesson is not "linear luminance was wrong", it is that *contrast measurement* and *tone mapping* are different questions.

### 17.2 Per-glyph occlusion

`_occlusion_fraction` measured what fraction of a text slot's **total** ink an art slot covers. That is the right question for "is this text crowded" and the wrong one for "is this text readable."

A word is about nine letters, so losing one whole letter costs the WORD about a ninth of its ink and costs the READER the word. On the "Tarmac" cover a cutout crested the title's baseline: whole-word occlusion measured 16.3%, comfortably inside the 30% limit, while the O was 100% buried and the R 44%. The cover said `LONGSW RD` and every gate passed.

**Fix:** `_glyph_boxes` cuts a fitted ink mask into one box per glyph — row runs for the lines, then column runs inside each line, via Pillow's `getprojection()`, thresholded at `_GLYPH_INK_FLOOR` so antialiasing cannot bridge two letters. `_worst_glyph_occlusion` returns the most-buried single glyph. `_occlusion_severity(whole, worst, limit)` combines both into one number where 1.0 is exactly at the limit, and all three guards (the sandwich guard, the line-gap snap, the general text/art contact pass) now accept, reject and **rank** candidates by it.

`_GLYPH_OCCLUSION_THRESHOLD` is 0.50 and is deliberately NOT scaled with each guard's whole-word limit: half a letter gone is an unreadable letter whichever guard is asking. The governing fact from the run — at a normal display weight, *any* bottom-edge overlap destroys a round letter, whose whole identity is its bowl.

Two reporting consequences: `RenderReport.occlusion` gains a `"{text}<-{art}#glyph"` key alongside the existing whole-word one (reported even when the sandwich passes, so an agent can see how close the worst letter came), and the degrade warning now names **which limit broke** — a buried letter and a crowded word are different defects with different fixes, and quoting the word's number for a letter's problem sends a reader looking in the wrong place.

Cost is not a concern: the per-glyph pass works on small crops and measures ~1.4ms against the whole-word measurement's ~16.7ms at full canvas size, and glyph boxes are computed once per text slot rather than per search candidate.

### 17.3 The dead band's blind axis

`_dead_band_frac` called a row dead when its **within-row** luminance stddev was under threshold. That measures one axis, and the structure a designer reaches for to fix a dead band lives on the other.

A full-bleed horizontal rule — the most ordinary way to put structure into an empty stretch — is perfectly uniform across its own row, so it scored as flat as the emptiness it was drawn to break. Worse than useless: a rule's row is *flatter* than a textured one, so drawing rules could raise the number. The "Scoreboard" cover's agent found this empirically — only its vertical column rules moved the metric, 0.327 → 0.120.

**Fix:** a row is alive if it varies across itself **or** its mean differs from a neighbour's by `_DEAD_BAND_ROW_DELTA_THRESHOLD` (0.020). A horizontal edge is a jump between adjacent rows, so that is where it has to be looked for. A gradient moves its mean by a hair per row and still measures dead, which is the behaviour this metric already had and wants to keep.



## 18. Archetype Three, and the two engine changes it needed (2026-09-01)

`portrait_luminary` — one lit face on the centre axis severed by the bottom trim, the book's own
landscape behind it in two silhouetted depth bands, one big soft light source off the axis, two
small saturated accent plates on a diagonal, author's name huge at the top and a display title
huge at the foot. Drawn from the contemporary romantasy portrait shelf; it takes the arrangement
and the craft from that shelf and none of the props.

Building it surfaced two things that were wrong in the engine rather than in the template.

### 18.1 A slot id is a label; the director needed a brief

`describe_archetypes()` — the ONLY thing an archetype ever tells the art-direction call — emitted
the `describe` line and each generatable slot's bare **id**. `ArchetypeArt.role` had existed since
the v2 BODY wave and reached no prompt anywhere: documentation whose only audience never saw it.

Told only that a template wants `luminary` and `token_near`, a model fills them from the nearest
cover it can remember rather than from the manuscript in front of it. That is the mechanism by
which a template stops being a template and becomes an impression of the one cover it was drawn
from — and it is invisible in a render, because the render looks fine.

**Fix:** `describe_archetypes()` emits `id — role` per generatable slot. `portrait_luminary` then
writes every role as a second-person question about the book (*"the sky this book actually happens
under. Name the hour, the weather and the world"*), so the template asks and the director answers.
`romantasy_organic` picks this up unchanged; its roles are terse nouns, and `hero — focal_subject`
still beats `hero`.

**The corollary, learned the expensive way.** Anything a template states as fact, every book wears.
The `hero` prompt frame originally ended its camera clause with *"the chin dropped slightly and the
eyes looking up through the brow"* and specified shadow *"in the eye sockets and under the jaw."*
That is not camera, it is ACTING — chin down, eyes up under the brow is the glower — and while it
sat in the frame, two covers sharing no prop, palette, setting or gender still came back wearing
one expression. The camera, the crop and the light geometry belong to the template. What is on the
character's face belongs to the book. Audit a prompt frame for smuggled performance before shipping
it.

### 18.2 Fixed placement is right for one cover and wrong for a catalogue

A one-shot template fixes placement so no book pays for it twice. That is the correct trade for a
single cover and a sameness generator across a list: two `portrait_luminary` covers with nothing
else in common still put their accent plates at byte-identical anchors, and a shelf of them reads
as one cover reprinted.

**Fix:** `TOKEN_LAYOUTS` — a closed shelf of four paired placements (`far_high_left`,
`far_high_right`, `far_low_left`, `far_low_right`) and a `Direction.token_layout` field
(`""` = the archetype's own anchors, so every existing archetype and archived direction is
untouched). `build_spec._token_anchors` applies it, and drops a pick the archetype cannot honour
with a log line rather than raising — the §6.1 surplus-prompt precedent.

Every layout keeps the pair on **opposite sides** and at **different heights**, guarded by a test:
the diagonal across the face is the composition and the corners are only its handedness, so a
layout that stacked them would be a different arrangement rather than a variation. The template
still owns scale, opacity, effects and z-order; placement is the one decision it delegates.

Addressed by the conventional slot ids `token_far` / `token_near`, exactly as `_intent_mask`'s
`"inside_focal"` addresses the conventional `focal` id.

### 18.3 Two known conflicts this wave did NOT fix

- `CUTOUT_SUFFIX` tells every transparent plate it is *"fully visible and complete"* while
  `_cut_edge_clause` tells the same plate it is severed and runs out of the picture. Every
  `cut_edge` slot on both shipped archetypes carries the contradiction; the plates come back right
  by weight of emphasis, not by agreement. The suffix should be cut-edge aware.
- `direction.py` forbids untreated photoreal art outright (`treatment: "none"` on a photoreal
  prompt is "never allowed, brief or no brief"), and `photo_soft` is a duotone that
  `portrait_luminary` cannot use — it flattens eight separately-lit plates into one sepia mass.
  The archetype is photoreal by construction, so the live director would refuse to fill it as
  written. The shelf-wide rule needs an archetype-scoped exemption.
