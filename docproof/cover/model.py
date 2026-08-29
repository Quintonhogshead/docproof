"""Cover Studio's data model: every pydantic type a cover job passes around.

The unit of work is a CoverSpec — a JSON document describing everything about
a cover except the raster art pixels. Coordinates are fractions of the canvas
(0-1), not pixels, so a spec is canvas-size-independent: the deferred print-
wrap phase re-targets the same spec at a different canvas without touching a
single number in it (see docs/cover_designer_spec.md §12). Every model here is
`extra="forbid"` — a stray key from a model call or a hand-edited spec fails
loudly at validation, not silently at render time three steps later.

build_spec() at the bottom is the seam between the other two foundation
modules: it merges one archetype's structure (docproof.cover.archetypes) with
one Direction's taste (palette, fonts, art prompts) and a Brief's words into a
renderable CoverSpec. Kept import-light on purpose — this module may import
.fonts and .archetypes, but direction.py/imaging.py/typeset.py/compose.py/
pipeline.py all import FROM here, never the reverse.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, create_model,
                      field_validator, model_validator)

from .archetypes import Archetype
from .fonts import FAMILIES

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_hex(value: str) -> str:
    """Every color in a spec is a literal #rrggbb string — no named colors,
    no alpha channel (alpha lives in its own field wherever a model has one)
    — so the composer never parses anything fancier than what Pillow's
    ImageColor.getrgb('#rrggbb') already expects."""
    if not _HEX_RE.match(value):
        raise ValueError(f"{value!r} is not a #rrggbb hex color")
    return value


class Brief(BaseModel):
    """The human input, fixed for the life of a job. Every downstream
    document (Direction, CoverSpec) is built from this plus a model's
    judgment — a revision edits the CoverSpec that came from it, never the
    brief itself."""
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    subtitle: str = ""
    author: str = Field(min_length=1)
    genre: str = Field(min_length=1)          # one of the 10 subject keys, or free text
    pitch: str = Field(default="", max_length=4000)
    mood: str = ""                            # comma phrases: "elegiac, wintry, hopeful"
    must_include: str = ""                    # "a lighthouse", "the color red"
    avoid: str = ""                           # "no faces, no dragons"
    concepts: int = Field(default=4, ge=1, le=6)


class PaletteRole(str, Enum):
    background = "background"
    primary = "primary"
    accent = "accent"
    text = "text"
    scrim = "scrim"


class Palette(BaseModel):
    """Five roles, five hexes — the only colors a spec ever names. Every
    other color reference (TextSlot.color_role, ScrimSpec.color_role) points
    here by role rather than repeating a hex, so a revision that "makes the
    palette warmer" touches five values once, not every slot that uses them."""
    model_config = ConfigDict(extra="forbid")

    background: str
    primary: str
    accent: str
    text: str
    scrim: str

    @field_validator("background", "primary", "accent", "text", "scrim")
    @classmethod
    def _hex(cls, value: str) -> str:
        return _validate_hex(value)

    def get(self, role: PaletteRole | str) -> str:
        """The hex for one role, by name — how the composer and the
        direction prompt both address a color. Raises ValueError (via
        PaletteRole's own constructor) for an unknown role name."""
        return getattr(self, PaletteRole(role).value)


class Zone(BaseModel):
    """A rectangle as a fraction of the canvas."""
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside_canvas(self) -> Zone:
        if self.x + self.w > 1.0 + 1e-6:
            raise ValueError(
                f"zone x+w={self.x + self.w:.4f} runs past the canvas's "
                f"right edge (x={self.x}, w={self.w})")
        if self.y + self.h > 1.0 + 1e-6:
            raise ValueError(
                f"zone y+h={self.y + self.h:.4f} runs past the canvas's "
                f"bottom edge (y={self.y}, h={self.h})")
        return self


class Shadow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dx: float = 0.0                            # fraction of canvas HEIGHT
    dy: float = 0.004
    blur: float = 0.006
    color: str = "#000000"
    alpha: float = Field(default=0.55, ge=0.0, le=1.0)

    @field_validator("color")
    @classmethod
    def _hex(cls, value: str) -> str:
        return _validate_hex(value)


class Stroke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: float = Field(default=0.0, ge=0.0)  # fraction of canvas height; 0 = off
    color: str = "#000000"

    @field_validator("color")
    @classmethod
    def _hex(cls, value: str) -> str:
        return _validate_hex(value)


class TextSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal["title", "subtitle", "author", "series"]
    content: str = ""                          # filled from Brief at spec-build time
    zone: Zone
    font_family: str                           # must exist in fonts.FAMILIES
    case: Literal["upper", "title", "as_is"] = "as_is"
    tracking: float = 0.0                      # em/1000 units (e.g. 120 = loose caps)
    align: Literal["left", "center", "right"] = "center"
    valign: Literal["top", "middle", "bottom"] = "middle"
    max_lines: int = Field(default=3, ge=1)
    size_min: float = Field(gt=0.0)            # fraction of canvas height
    size_max: float = Field(gt=0.0)
    color_role: PaletteRole = PaletteRole.text
    shadow: Shadow | None = None
    stroke: Stroke | None = None
    optional: bool = False                     # subtitle/series render only if content

    @field_validator("font_family")
    @classmethod
    def _known_family(cls, value: str) -> str:
        if value not in FAMILIES:
            raise ValueError(
                f"font_family {value!r} is not registered — known families: "
                f"{', '.join(sorted(FAMILIES))}")
        return value

    @model_validator(mode="after")
    def _size_range(self) -> TextSlot:
        if self.size_min > self.size_max:
            raise ValueError(
                f"size_min ({self.size_min}) exceeds size_max "
                f"({self.size_max})")
        return self


class ArtSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal["background", "focal", "texture"]
    prompt: str = ""                           # what to ask gpt-image-2 for; "" = procedural
    transparent: bool = False                  # request transparent background (cutouts)
    fit: Literal["cover", "contain"] = "cover"
    # Pairs are lists, not tuples: OpenAI's strict structured-output mode
    # rejects tuple-derived prefixItems schemas ("array schema missing
    # items"), and CoverSpec IS the revision call's wire schema (§6.2).
    anchor: list[float] = Field(                # focal point kept in frame when cover-cropping
        default_factory=lambda: [0.5, 0.5])
    scale: float = Field(default=1.0, gt=0.0)  # post-fit zoom, ~1.0..1.4
    offset: list[float] = Field(                # fraction nudge after fit
        default_factory=lambda: [0.0, 0.0])

    @field_validator("anchor", "offset")
    @classmethod
    def _pair(cls, value: list[float]) -> list[float]:
        if len(value) != 2:
            raise ValueError("anchor/offset must be exactly [x, y]")
        if not all(-2.0 <= v <= 2.0 for v in value):
            raise ValueError("anchor/offset values must stay within [-2, 2]")
        return value
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    blend: Literal["normal", "multiply", "overlay", "soft_light"] = "normal"
    asset: str = ""                            # relative path under the job dir once generated


class ScrimSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["gradient_down", "gradient_up", "vignette", "panel"] = "panel"
    zone: Zone | None = None                   # None = derived from the protected TextSlot
    protects: Literal["title", "subtitle", "author", "series"] | None = None
    strength: float = Field(default=0.0, ge=0.0, le=1.0)
    color_role: PaletteRole = PaletteRole.scrim


class LayerRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["art", "scrim", "text"]
    ref: str                                    # ArtSlot.id / scrim index / TextSlot.id


class CoverSpec(BaseModel):
    """The full renderable document for one direction. Spec + generated
    assets -> pixel-identical render, every time — archive the spec and any
    cover is reproducible and hand-editable forever."""
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)      # bump on every revision
    archetype: str                              # archetype file stem
    concept_name: str                           # "Ash and Brass"
    rationale: str                              # one sentence, shown on the card
    palette: Palette
    art: list[ArtSlot]
    scrims: list[ScrimSpec]
    text: list[TextSlot]
    layers: list[LayerRef]                      # explicit z-order, bottom first
    notes_log: list[str] = Field(default_factory=list)


class RenderReport(BaseModel):
    """What one compose() call found, for the "did this actually come out
    legible" check — never guessed at, always measured (see
    docs/cover_designer_spec.md §7.3's legibility autopilot)."""
    model_config = ConfigDict(extra="forbid")

    contrast: dict[str, float]                  # text slot id -> achieved contrast ratio
    scrim_final: dict[int, float]                # scrim index -> strength after escalation
    fitted_sizes: dict[str, float]                # slot id -> chosen size (fraction)
    warnings: list[str]                          # "title at size_min and still 2 lines over"


# -- the art-direction call's answer -----------------------------------------

# The registry's family names, fixed at import time — built via create_model
# exactly the way docproof.prep.meta.detect_meta builds BookFacts.subject as
# a Literal over BookDesign.subject_choices: a family that does not exist on
# the shelf cannot be picked on the wire, schema-enforced rather than merely
# hoped for.
_FONT_FAMILY_NAMES: tuple[str, ...] = tuple(FAMILIES)


class ArtPrompt(BaseModel):
    """One generatable art slot's image prompt, as a typed pair rather than a
    free-form mapping: OpenAI's strict structured-output mode rejects schemas
    with open-ended object keys (every property must be enumerated and
    required), so the wire shape is a list of these. Callers that find a dict
    more natural may still pass one — Direction converts it on validation."""
    model_config = ConfigDict(extra="forbid")

    slot: Literal["background", "focal", "texture"]
    prompt: str


def _coerce_art_prompts(value: object) -> object:
    """Accept the {slot: prompt} dict shape everywhere except the wire."""
    if isinstance(value, dict):
        return [{"slot": k, "prompt": v} for k, v in value.items()]
    return value


Direction = create_model(
    "Direction",
    __config__=ConfigDict(extra="forbid"),
    __doc__=(
        "One design concept from the art-direction call: an archetype pick, "
        "a palette, a title/author font pick each (constrained to "
        "fonts.FAMILIES), an image prompt per generatable art slot the "
        "chosen archetype declares, and whether to add the optional texture "
        "layer. See docs/cover_designer_spec.md §6.1."),
    concept_name=(str, ...),                   # "Ash and Brass"
    rationale=(str, ...),                       # one sentence, shown on the card
    archetype=(str, ...),                       # an archetypes.ARCHETYPES key
    palette=(Palette, ...),
    title_font=(Literal[*_FONT_FAMILY_NAMES], ...),
    author_font=(Literal[*_FONT_FAMILY_NAMES], ...),
    art_prompts=(list[ArtPrompt], ...),         # one entry per generatable slot
    texture=(bool, ...),
    __validators__={
        "_art_prompts_dict_ok": field_validator(
            "art_prompts", mode="before")(_coerce_art_prompts),
    },
)


class Directions(BaseModel):
    """One art-direction call's whole answer: every concept together, so the
    model can deliberately keep them distinct from each other rather than
    drifting toward one idea concept by concept (see §6.1)."""
    model_config = ConfigDict(extra="forbid")

    concepts: list[Direction] = Field(min_length=1, max_length=6)


# -- job/concept persistence (§8) --------------------------------------------

class ConceptState(BaseModel):
    """One direction's progress through painting and composing. Lives inside
    JobState, which is rewritten to job.json after every step, so a poll — or
    a machine restart — always sees the truth."""
    model_config = ConfigDict(extra="forbid")

    spec: CoverSpec
    status: Literal["queued", "painting", "composing", "ready", "error"]
    error: str | None = None
    report: RenderReport | None = None
    renders: list[str] = Field(default_factory=list)   # relative paths, newest last


class JobState(BaseModel):
    """One book's cover session: brief -> N directions -> renders ->
    revisions. The single source of truth for a job, serialized to job.json
    after every step (see docs/cover_designer_spec.md §8)."""
    model_config = ConfigDict(extra="forbid")

    job_id: str                                 # UTC timestamp + 6 hex chars
    brief: Brief
    manuscript_name: str = ""                   # set only when a manuscript was uploaded
    word_count: int = 0
    status: Literal["directing", "working", "ready", "error"]
    error: str | None = None
    concepts: list[ConceptState] = Field(default_factory=list)
    ledger: list[dict[str, Any]] = Field(default_factory=list)   # {kind, detail, usd}
    created: str                                 # ISO UTC


# -- the merge: archetype + direction + brief -> spec ------------------------

def build_spec(direction: Direction, brief: Brief, archetype: Archetype) -> CoverSpec:
    """Merge one art-direction concept into its chosen archetype's template.

    The archetype supplies structure (zones, fitting rules, layer order); the
    direction supplies taste (palette, fonts, art prompts, whether to add
    texture); the brief supplies the words. `archetype` must be the Archetype
    named by `direction.archetype` — the caller (docproof.cover.pipeline's
    run_job) resolves that lookup once from archetypes.ARCHETYPES and passes
    the same object here rather than making build_spec re-fetch it.

    `title` gets `direction.title_font`; every other text slot (subtitle,
    author, and series if a future archetype adds one) gets
    `direction.author_font` — the direction call only ever picks two fonts,
    one hero face and one supporting face, so every non-title slot shares
    the second."""
    if direction.archetype != archetype.name:
        raise ValueError(
            f"build_spec: direction picked archetype {direction.archetype!r} "
            f"but was given archetype {archetype.name!r}")

    include_texture = bool(direction.texture)
    prompts = {p.slot: p.prompt for p in direction.art_prompts}

    art: list[ArtSlot] = []
    for slot in archetype.art:
        if slot.id == "texture" and not include_texture:
            continue
        art.append(ArtSlot(
            id=slot.id,
            prompt=(prompts.get(slot.id, "")
                    if slot.generatable else ""),
            transparent=slot.transparent,
            fit=slot.fit,
            opacity=slot.opacity,
            blend=slot.blend,
            anchor=slot.anchor,
            scale=slot.scale,
            offset=slot.offset))

    scrims = [ScrimSpec(kind=s.kind, protects=s.protects, strength=s.strength)
             for s in archetype.scrims]

    text: list[TextSlot] = []
    for slot in archetype.text:
        font = direction.title_font if slot.id == "title" else direction.author_font
        text.append(TextSlot(
            id=slot.id,
            content=getattr(brief, slot.id, ""),
            zone=Zone(x=slot.zone.x, y=slot.zone.y, w=slot.zone.w, h=slot.zone.h),
            font_family=font,
            case=slot.case,
            tracking=slot.tracking,
            align=slot.align,
            valign=slot.valign,
            max_lines=slot.max_lines,
            size_min=slot.size_min,
            size_max=slot.size_max,
            shadow=Shadow(**slot.shadow.model_dump()) if slot.shadow else None,
            stroke=Stroke(**slot.stroke.model_dump()) if slot.stroke else None,
            optional=slot.optional))

    art_ids = {a.id for a in archetype.art}
    layers: list[LayerRef] = []
    for ref in archetype.layers:
        if ref == "texture" and not include_texture:
            continue
        if ref.startswith("scrim:"):
            layers.append(LayerRef(kind="scrim", ref=ref.removeprefix("scrim:")))
        elif ref in art_ids:
            layers.append(LayerRef(kind="art", ref=ref))
        else:
            layers.append(LayerRef(kind="text", ref=ref))

    return CoverSpec(
        archetype=archetype.name,
        concept_name=direction.concept_name,
        rationale=direction.rationale,
        palette=direction.palette,
        art=art, scrims=scrims, text=text, layers=layers)


__all__ = [
    "Brief", "PaletteRole", "Palette", "Zone", "Shadow", "Stroke",
    "TextSlot", "ArtSlot", "ScrimSpec", "LayerRef", "CoverSpec",
    "RenderReport", "Direction", "Directions", "ConceptState", "JobState",
    "build_spec",
]
