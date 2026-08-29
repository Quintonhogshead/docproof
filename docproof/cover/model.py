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

# v2 BODY wave: an ArtSlot/ArtPrompt/ArchetypeArt id is any lowercase slug
# matching this pattern — widened from the launch/effects-rack closed Literal
# five (see ART_SLOT_IDS below) so an archetype can name a slot for what it
# IS ("vine_left", "emblem", "border_motif") instead of contorting every
# decorative layer into "focal2"/"foreground". 1-24 chars, lowercase letters/
# digits/underscore, must start with a letter (so a slug can never be
# confused with a scrim's "scrim:N" layer-ref shorthand or a bare digit).
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def _validate_slot_id(value: str) -> str:
    if not _SLOT_ID_RE.match(value):
        raise ValueError(
            f"{value!r} is not a valid art slot id — must match "
            f"{_SLOT_ID_RE.pattern!r} (lowercase letters/digits/underscore, "
            f"starting with a letter, at most 24 characters)")
    return value


def _validate_hex(value: str) -> str:
    """Every color in a spec is a literal #rrggbb string — no named colors,
    no alpha channel (alpha lives in its own field wherever a model has one)
    — so the composer never parses anything fancier than what Pillow's
    ImageColor.getrgb('#rrggbb') already expects."""
    if not _HEX_RE.match(value):
        raise ValueError(f"{value!r} is not a #rrggbb hex color")
    return value


# The five names every launch/effects-rack archetype already used, back when
# ArtSlot.id/ArtPrompt.slot were a closed Literal over exactly this tuple.
# The v2 BODY wave widened both to _SLOT_ID_RE (any lowercase slug) — these
# five still validate unchanged (they're just slugs now, like any other), and
# this tuple survives purely as a documented "the legacy names, for
# reference" constant. archetypes.ArchetypeArt.id mirrors the same widening
# independently — see that module's docstring for why it never imports this
# one.
ART_SLOT_IDS: tuple[str, ...] = ("background", "focal", "focal2", "foreground", "texture")

# The slot treatments every ArtSlot/ArtPrompt may request (§7.4a): pure,
# deterministic Pillow ops compose.py applies after fit/placement and before
# compositing. "none" is the default on every launch archetype and every
# archetype this session did not explicitly retrofit — the rack is opt-in,
# never a surprise on an existing cover.
ART_TREATMENTS: tuple[str, ...] = ("none", "duotone", "silhouette", "posterize", "sticker")

# The named procedural synthesizers (v2 BODY wave) an ArchetypeArt/ArtSlot
# may request via `procedural: <name>` instead of (or as the no-asset
# fallback alongside) an AI-generated `prompt` — compose.py's
# PROCEDURAL_SYNTHESIZERS dict is the other half of this contract, one pure
# function per name, keyed on exactly these strings. This tuple is the
# single source of truth for which names are legal (mirroring how
# ART_TREATMENTS is the source of truth for `treatment`, even though the
# pixel logic for both lives downstream in compose.py) — a typo'd name fails
# loudly at spec-validation/archetype-load time, not silently as a blank
# layer three steps later in compose().
PROCEDURAL_KINDS: tuple[str, ...] = (
    "gradient", "grain", "paper", "halftone", "canvas", "speckle", "rule_frame")


def _validate_scatter(value: int) -> int:
    """`scatter` is either off (0) or a real stamped count — 1 copy would
    just be a worse-positioned normal placement, so the closed range starts
    at 2 (§7.4a: "ArtSlot.scatter: int = 0 (transparent slots, 2-12)"),
    matching the same "a lone value is not a range" reasoning ScrimSpec's
    strength bounds and Brief.concepts already apply elsewhere in this
    module."""
    if value != 0 and not (2 <= value <= 12):
        raise ValueError(
            f"scatter must be 0 (off) or between 2 and 12, got {value}")
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
    # "fill" (default) is typeset.draw_text's normal ink-colored glyphs.
    # "knockout"/"art_fill" (§7.4a) are archetype/revision territory, never
    # the art-direction call's (direction.py says so explicitly) — a fresh
    # concept always starts "fill", and only a hand-authored archetype or a
    # human revision's notes ever choose otherwise.
    mode: Literal["fill", "knockout", "art_fill"] = "fill"
    # "thing inside of thing" (v2 BODY wave): the id of an ArtSlot whose
    # POSITIONED alpha this text is clipped to — a title living inside a
    # lighthouse beam, an image inside a train's smoke plume. "" = off (draw
    # normally). Archetype/revision territory, same bucket as mode: no
    # Direction field ever sets it. Unlike ArtSlot.mask_from, the referenced
    # slot need NOT precede this text slot in `layers` — see CoverSpec's own
    # _text_mask_from_resolves for why draw order doesn't matter here.
    mask_from: str = ""

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

    id: str                                     # any slug matching _SLOT_ID_RE
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
    # Names a compose.PROCEDURAL_SYNTHESIZERS entry to draw when this slot
    # has no `asset` on disk (v2 BODY wave). "" = no opinion — a slot with
    # id "background"/"texture" then falls back to the ORIGINAL hardcoded-by-
    # id behavior (gradient / grain respectively) so every pre-existing
    # YAML/spec keeps rendering byte-identical pixels; any other id with ""
    # draws nothing, exactly like before this field existed. A non-"" name
    # applies regardless of id — including a GENERATABLE slot whose asset
    # never arrived, which is a graceful, designed fallback rather than a
    # blank layer.
    procedural: Literal["", "gradient", "grain", "paper", "halftone",
                        "canvas", "speckle", "rule_frame"] = ""

    # -- effects rack (§7.4a) — archetype/revision territory; a fresh
    # art-direction call only ever sets `treatment` (via ArtPrompt, folded in
    # by build_spec), never these four directly. ------------------------------
    treatment: Literal["none", "duotone", "silhouette", "posterize",
                       "sticker"] = "none"
    mask_from: str = ""                        # another art slot's id, or "" = off
    corners: bool = False                      # mirror into all four corners (transparent slots)
    scatter: int = Field(default=0)            # stamp N copies, 0 = off (transparent slots)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_slot_id(value)

    @field_validator("scatter")
    @classmethod
    def _scatter_range(cls, value: int) -> int:
        return _validate_scatter(value)


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

    @model_validator(mode="after")
    def _layers_resolve(self) -> CoverSpec:
        """Every layers entry must resolve to a real art slot, text slot, or
        scrim index — the same guarantee archetypes.Archetype enforces on its
        own layer list, re-enforced here because a revision (§6.2) hands the
        WHOLE spec to a model for rewriting: dropping a slot while leaving
        its layer reference behind must fail as a readable validation error,
        not as compose()'s bare KeyError three steps later."""
        art_ids = {a.id for a in self.art}
        text_ids = {t.id for t in self.text}
        for ref in self.layers:
            if ref.kind == "art" and ref.ref not in art_ids:
                raise ValueError(
                    f"layers references art slot {ref.ref!r}, which is not "
                    f"in this spec's art list "
                    f"({', '.join(sorted(art_ids)) or 'empty'})")
            if ref.kind == "text" and ref.ref not in text_ids:
                raise ValueError(
                    f"layers references text slot {ref.ref!r}, which is not "
                    f"in this spec's text list "
                    f"({', '.join(sorted(text_ids)) or 'empty'})")
            if ref.kind == "scrim":
                if not ref.ref.isdigit() or int(ref.ref) >= len(self.scrims):
                    raise ValueError(
                        f"layers references scrim {ref.ref!r}, but this "
                        f"spec has {len(self.scrims)} scrim(s)")
        return self

    @model_validator(mode="after")
    def _mask_from_precedes(self) -> CoverSpec:
        """§7.4a: "the referenced slot must exist and precede it in `layers`;
        a dangling reference fails spec validation." Checked here — at the
        whole-spec level, not on ArtSlot alone — because "precedes" only
        means anything relative to `layers`, which ArtSlot itself can't see.
        A revision (§6.2) hands the model the WHOLE spec and validates the
        result with this same model, so a revision that breaks this
        invariant is caught exactly the same way a fresh build_spec would
        be."""
        art_ids = {a.id for a in self.art}
        first_art_position: dict[str, int] = {}
        for i, ref in enumerate(self.layers):
            if ref.kind == "art" and ref.ref not in first_art_position:
                first_art_position[ref.ref] = i
        for slot in self.art:
            if not slot.mask_from:
                continue
            if slot.mask_from not in art_ids:
                raise ValueError(
                    f"art slot {slot.id!r} has mask_from={slot.mask_from!r}, "
                    f"which is not an art slot in this spec (art slots: "
                    f"{', '.join(sorted(art_ids))})")
            this_pos = first_art_position.get(slot.id)
            ref_pos = first_art_position.get(slot.mask_from)
            if this_pos is None or ref_pos is None or ref_pos >= this_pos:
                raise ValueError(
                    f"art slot {slot.id!r}'s mask_from={slot.mask_from!r} "
                    f"must appear earlier in `layers` than {slot.id!r} "
                    f"itself, so its pixels are already positioned by the "
                    f"time {slot.id!r} is drawn")
        return self

    @model_validator(mode="after")
    def _text_mask_from_resolves(self) -> CoverSpec:
        """"Thing inside of thing" (v2 BODY wave): a TextSlot.mask_from must
        name a real art slot in THIS spec — but, unlike ArtSlot's own
        mask_from (_mask_from_precedes, above), draw ORDER never matters
        here. compose() positions every art slot's final pixels once, up
        front, before any text is measured or drawn (see
        compose._position_all_art) — a container art slot is available for
        clipping whether it is drawn under the text, or later, over it, for
        a weave. So this checks existence only, deliberately with no
        "precedes" requirement to mirror."""
        art_ids = {a.id for a in self.art}
        for slot in self.text:
            if slot.mask_from and slot.mask_from not in art_ids:
                raise ValueError(
                    f"text slot {slot.id!r} has mask_from="
                    f"{slot.mask_from!r}, which is not an art slot in this "
                    f"spec (art slots: {', '.join(sorted(art_ids)) or 'empty'})")
        return self


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

    slot: str                                   # any slug matching _SLOT_ID_RE
    prompt: str
    # The ONLY effects-rack field the art-direction call may set (§7.4a) —
    # build_spec folds this onto the matching ArtSlot.treatment for a
    # generatable slot; mask_from/corners/scatter/TextSlot.mode stay
    # archetype/revision territory (direction.py's system prompt says so).
    treatment: Literal["none", "duotone", "silhouette", "posterize",
                       "sticker"] = "none"

    @field_validator("slot")
    @classmethod
    def _valid_slot(cls, value: str) -> str:
        return _validate_slot_id(value)


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
    # "none" from the model reads as "no opinion", not "force no effect": an
    # archetype that presets a treatment (the cozy_mystery_graphic_stamp icon
    # -> silhouette retrofit, say) keeps that convention by default, and only
    # a direction that actually NAMES an effect overrides it. Direction never
    # sees mask_from/corners/scatter/TextSlot.mode at all (§7.4a: "archetype/
    # revision territory") — those always come straight from the archetype.
    prompt_treatments = {p.slot: p.treatment for p in direction.art_prompts
                        if p.treatment != "none"}

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
            offset=slot.offset,
            treatment=prompt_treatments.get(slot.id, slot.treatment),
            mask_from=slot.mask_from,
            corners=slot.corners,
            scatter=slot.scatter,
            procedural=slot.procedural))

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
            optional=slot.optional,
            mode=slot.mode,
            mask_from=slot.mask_from))

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
    "ART_SLOT_IDS", "ART_TREATMENTS", "PROCEDURAL_KINDS",
    "Brief", "PaletteRole", "Palette", "Zone", "Shadow", "Stroke",
    "TextSlot", "ArtSlot", "ScrimSpec", "LayerRef", "CoverSpec",
    "RenderReport", "Direction", "Directions", "ConceptState", "JobState",
    "build_spec",
]
