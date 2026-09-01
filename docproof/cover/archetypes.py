"""Cover archetypes: parametric layout templates, as data, not code.

An archetype is everything about a cover's STRUCTURE that does not depend on
one book's brief: which art slots exist and whether they are AI-generatable or
procedural, where the text zones sit and how they may fit, the default scrims
that protect them, the layer order, and a composition_note steered into the
background image prompt so the art itself leaves room for the type. They ship
in config/cover/archetypes/*.yaml: three untagged launch archetypes plus a
genre-tagged library grown from researched bestseller conventions (see
docs/cover_designer_spec.md §5, §5.3); loading and validating them is this
module's whole job.

docproof.cover.model.build_spec() is the other half: it merges one archetype
(structure) with one Direction (taste — palette, fonts, art prompts) and a
Brief (words) into a renderable CoverSpec. That merge lives in model.py, not
here, on purpose — model.py is allowed to import this module, and this module
must stay importable without model.py, so the models below intentionally
mirror model.py's Zone/Shadow/Stroke shapes rather than reusing them.

A malformed archetype file fails LOUDLY at import: a launch archetype is core
inventory, not an optional extra, and a broken one should break the build
rather than silently vanish from the direction call's enumerated list.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_validator)

from .recipes import RECIPES
from .textures import TEXTURES

# docproof/cover/archetypes.py -> docproof/cover -> docproof -> package root,
# the same depth docproof/eval/candidate_eval.py's CANDIDATE_CASES walks, and
# the same pattern docproof/genre.py's _genres_dir() and docproof/stages.py's
# _stages_dir() use for their own config subtrees. docproof must not import
# app.settings.resource_root() — it is the library layer, app is not.
ARCHETYPES_DIR = Path(__file__).resolve().parents[2] / "config" / "cover" / "archetypes"

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Mirrors docproof.cover.model._SLOT_ID_RE/_validate_slot_id exactly (same
# widening, same reasoning: any lowercase slug, 1-24 chars, starting with a
# letter) — duplicated rather than imported for the same reason this whole
# module mirrors Zone/Shadow/Stroke instead of importing them (see the module
# docstring).
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def _validate_slot_id(value: str) -> str:
    if not _SLOT_ID_RE.match(value):
        raise ValueError(
            f"{value!r} is not a valid art slot id — must match "
            f"{_SLOT_ID_RE.pattern!r} (lowercase letters/digits/underscore, "
            f"starting with a letter, at most 24 characters)")
    return value

# The ten subject keys a brief's genre (or an archetype's `genres:` tag) may
# name — the same closed list config/prep/book_design.yaml's `subjects:`
# section and app/static/sc-cover.html's genre <select> use, mirrored here
# (not imported: docproof must not import app.settings, and book_design's
# subject set is data-driven per design file rather than a fixed constant) so
# an archetype's genres list and a Brief's genre can be checked against
# exactly the same ten strings (docs/cover_designer_spec.md §5.3).
SUBJECT_KEYS: frozenset[str] = frozenset({
    "fantasy", "science_fiction", "romance", "mystery_thriller", "horror",
    "historical", "literary", "memoir_biography", "nonfiction",
    "young_readers",
})


def _hex(value: str) -> str:
    if not _HEX_RE.match(value):
        raise ValueError(f"{value!r} is not a #rrggbb hex color")
    return value


# The five PaletteRole names, mirrored from docproof.cover.model rather than
# imported for the same reason this whole module mirrors Zone/Shadow/Stroke
# (see the module docstring) — what ArchetypeEffect's role-or-hex color
# references validate against.
_ROLE_NAMES: frozenset[str] = frozenset(
    {"background", "primary", "accent", "text", "scrim"})


def _role_or_hex(value: str) -> str:
    """Mirrors docproof.cover.model._validate_role_or_hex exactly: a color
    reference is either one of the five palette role names or a literal
    #rrggbb hex, and anything else fails at load with both legal shapes
    named."""
    if value in _ROLE_NAMES or _HEX_RE.match(value):
        return value
    raise ValueError(
        f"{value!r} is neither a palette role "
        f"({', '.join(sorted(_ROLE_NAMES))}) nor a #rrggbb hex color")


# Mirrors docproof.cover.model.FX_PREFIX (§15.6): reserved for
# recipe-expanded layers, so a hand-authored archetype slot may never wear
# it — ArchetypeArt._valid_id refuses it at load, which is what keeps a
# recipe's expansion collision-free by construction.
_FX_PREFIX = "fx_"


def _scatter_range(value: int) -> int:
    """Mirrors docproof.cover.model._validate_scatter exactly (same
    reasoning: 0 is off, 1 is not a real "scatter", so the closed range
    starts at 2) — duplicated rather than imported for the same reason this
    whole module mirrors Zone/Shadow/Stroke instead of importing them (see
    the module docstring)."""
    if value != 0 and not (2 <= value <= 12):
        raise ValueError(
            f"scatter must be 0 (off) or between 2 and 12, got {value}")
    return value


class ArchetypeError(Exception):
    """An archetype file that cannot be used — missing directory, unreadable
    YAML, or a shape the Archetype model rejects. The message always names
    the file, because whoever hits this is editing a YAML template, not
    Python (see docproof.prep.book_design.BookDesignError for the same
    convention)."""


class ArchetypeZone(BaseModel):
    """A rectangle as a fraction of the canvas. Field-for-field the same
    shape as docproof.cover.model.Zone (kept separate so this module never
    has to import model.py — see the module docstring); build_spec converts
    one of these into a real Zone when it expands an archetype's text slot."""
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside_canvas(self) -> ArchetypeZone:
        if self.x + self.w > 1.0 + 1e-6:
            raise ValueError(
                f"zone x+w={self.x + self.w:.4f} runs past the canvas's "
                f"right edge (x={self.x}, w={self.w})")
        if self.y + self.h > 1.0 + 1e-6:
            raise ValueError(
                f"zone y+h={self.y + self.h:.4f} runs past the canvas's "
                f"bottom edge (y={self.y}, h={self.h})")
        return self


class ArchetypeShadow(BaseModel):
    """Mirrors docproof.cover.model.Shadow's fields and defaults exactly;
    build_spec passes one of these straight into a real Shadow."""
    model_config = ConfigDict(extra="forbid")

    dx: float = 0.0
    dy: float = 0.004
    blur: float = 0.006
    color: str = "#000000"
    alpha: float = Field(default=0.55, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _valid_hex(self) -> ArchetypeShadow:
        _hex(self.color)
        return self


class ArchetypeStroke(BaseModel):
    """Mirrors docproof.cover.model.Stroke's fields and defaults exactly."""
    model_config = ConfigDict(extra="forbid")

    width: float = Field(default=0.0, ge=0.0)
    color: str = "#000000"

    @model_validator(mode="after")
    def _valid_hex(self) -> ArchetypeStroke:
        _hex(self.color)
        return self


class ArchetypeEffect(BaseModel):
    """Mirrors docproof.cover.model.Effect's fields, defaults, and
    validation exactly (§15.4) — one entry in a designed layer-style stack
    an archetype bakes in (the thriller title's stacked double shadow);
    build_spec passes each straight into a real Effect. Same forgiving-
    fields rule: params the chosen `kind` never reads are validated but
    inert."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["drop_shadow", "inner_shadow", "outer_glow", "inner_glow",
                  "bevel", "gradient_overlay", "texture_overlay", "stroke"]
    dx: float = 0.0
    dy: float = 0.004
    blur: float = Field(default=0.006, ge=0.0)
    color: str = ""
    alpha: float = Field(default=0.55, ge=0.0, le=1.0)
    width: float = Field(default=0.004, ge=0.0)
    stops: list[str] = Field(default_factory=list)
    angle: float = 90.0
    texture_file: str = ""
    blend: Literal["normal", "multiply", "overlay", "soft_light", "screen",
                   "add", "lighten", "darken", "color_dodge"] = "normal"
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        return _role_or_hex(value) if value else value

    @field_validator("stops")
    @classmethod
    def _valid_stops(cls, value: list[str]) -> list[str]:
        if len(value) not in (0, 2, 3):
            raise ValueError(
                f"stops must have 2 or 3 entries (or be empty when unused), "
                f"got {len(value)}")
        return [_role_or_hex(stop) for stop in value]

    @field_validator("texture_file")
    @classmethod
    def _known_texture(cls, value: str) -> str:
        if value and value not in TEXTURES:
            raise ValueError(
                f"texture_file {value!r} is not on the shelf — known "
                f"textures: {', '.join(sorted(TEXTURES)) or 'none'}")
        return value

    @model_validator(mode="after")
    def _kind_requirements(self) -> ArchetypeEffect:
        if self.kind == "gradient_overlay" and not self.stops:
            raise ValueError(
                "a gradient_overlay effect needs 2 or 3 stops (role names "
                "or #rrggbb hexes, dark to light)")
        if self.kind == "texture_overlay" and not self.texture_file:
            raise ValueError(
                "a texture_overlay effect needs a texture_file from the "
                f"shelf ({', '.join(sorted(TEXTURES)) or 'none stocked'})")
        return self


class ArchetypeGradientMask(BaseModel):
    """Mirrors docproof.cover.model.GradientMask's fields, defaults, and
    validation exactly (§15.2) — kept separate so this module never imports
    model.py, the ArchetypeZone/ArchetypeShadow reasoning. build_spec passes
    one of these (via ArchetypeMask below) straight into a real
    GradientMask."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["linear", "radial"] = "linear"
    angle: float = 90.0
    center: list[float] = Field(default_factory=lambda: [0.5, 0.5])
    start: float = Field(default=0.0, ge=0.0, le=1.0)
    end: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("center")
    @classmethod
    def _pair(cls, value: list[float]) -> list[float]:
        if len(value) != 2:
            raise ValueError("center must be exactly [x, y]")
        if not all(-2.0 <= v <= 2.0 for v in value):
            raise ValueError("center values must stay within [-2, 2]")
        return value

    @model_validator(mode="after")
    def _ramp_direction(self) -> ArchetypeGradientMask:
        if self.start >= self.end:
            raise ValueError(
                f"gradient mask start ({self.start}) must be strictly less "
                f"than end ({self.end})")
        return self


class ArchetypeMask(BaseModel):
    """Mirrors docproof.cover.model.MaskSpec exactly (§15.2/§15.13): a
    first-class mask an archetype bakes onto one of its own art slots — the
    enabler for the mask-forward templates (§15.13 part 3: title_window's
    art-in-the-glyphs `from_text`, split_plate's two-plates-into-one-scene
    `gradient`), which the legacy single-stencil `mask_from` sugar cannot
    express. Existence/ordering rules against this archetype's own slots
    are checked at load time by Archetype._art_masks_resolve below (the
    module's "fails LOUDLY at import" philosophy); the built spec is then
    re-validated by CoverSpec's own deeper mask validators anyway, so the
    two layers can never disagree for long."""
    model_config = ConfigDict(extra="forbid")

    from_layer: str = ""
    gradient: ArchetypeGradientMask | None = None
    luminance_of: str = ""
    from_text: str = ""
    invert: bool = False
    # Mirrors docproof.cover.model.MaskSpec.feather exactly: a Gaussian blur
    # of the RESOLVED mask, as a fraction of canvas height. The one knob
    # that softens the hard 50%-threshold `from_layer` stencil.
    feather: float = Field(default=0.0, ge=0.0, le=0.25)

    @field_validator("from_layer", "luminance_of")
    @classmethod
    def _valid_slot_ref(cls, value: str) -> str:
        return _validate_slot_id(value) if value else value

    @model_validator(mode="after")
    def _some_source(self) -> ArchetypeMask:
        if not (self.from_layer or self.gradient is not None
                or self.luminance_of or self.from_text):
            raise ValueError(
                "mask sets no source — set at least one of from_layer, "
                "gradient, luminance_of, or from_text (or drop the mask)")
        return self


class ArchetypeArt(BaseModel):
    """One art slot an archetype declares. `generatable` says whether the
    art-direction call is asked to write an image prompt for this slot at
    all — a false slot is always synthesized procedurally by the composer
    (docs/cover_designer_spec.md §7.3), never sent to gpt-image-2. The rest
    are the composer's defaults for that slot until a Direction or a revision
    overrides them (a Direction never touches fit/opacity/blend; a revision
    can)."""
    model_config = ConfigDict(extra="forbid")

    id: str                                     # any slug matching _SLOT_ID_RE
    generatable: bool
    # Free-text documentation of what this slot IS for, structurally
    # ("focal_subject", "far_tier", "corner_fill"). Never read by the engine;
    # it exists so a template can name its slots for their ROLE rather than
    # for whatever noun the first book to use it happened to put there.
    role: str = ""
    # The one-shot contract (paired with `cut_edge` below). The plate does
    # not exist when a template is authored, so the template cannot know
    # which edge the generator will sever a stem on — it has to DICTATE it.
    # This is a sentence carrying exactly one `{subject}` hole; the
    # art-direction call fills the hole with a noun and nothing else, and
    # pipeline._assemble_prompt expands the frame (plus the `cut_edge`
    # clause) around it. "" keeps the pre-existing behaviour: the direction's
    # prompt is used verbatim.
    prompt_frame: str = ""
    # Which edge of its own frame this plate's severed end must sit on, so
    # the fixed `anchor`/`offset` below can carry that cut off the canvas.
    # Expanded into an explicit instruction appended to the prompt. "" = the
    # slot has no severed end to place (a mat, a particle field, a loose
    # object).
    cut_edge: Literal["", "top", "bottom", "left", "right", "top_left",
                      "top_right", "bottom_left", "bottom_right"] = ""
    # Mirrors docproof.cover.model.ArtSlot.mirror exactly: flip the plate
    # horizontally before fit and placement, so a severed stem that points
    # into the frame can be turned to run out of it.
    mirror: bool = False
    fit: Literal["cover", "contain"] = "cover"
    transparent: bool = False
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    # The FULL model.BLEND_MODES table, matching ArtSlot.blend exactly. This
    # used to stop at soft_light, which meant a template could not declare a
    # light-emitting layer at all — a glow or a drifting ember field had to
    # be patched onto the built spec afterwards, outside the template.
    blend: Literal["normal", "multiply", "overlay", "soft_light", "screen",
                   "add", "lighten", "darken", "color_dodge"] = "normal"
    # Placement defaults for contain-fit slots (a full-canvas cutout buries
    # the title it was meant to overlap — the archetype pins where the
    # figure sits; a revision may still move it). Same semantics as
    # ArtSlot's fields of the same names.
    anchor: list[float] = Field(default_factory=lambda: [0.5, 0.5])
    scale: float = Field(default=1.0, gt=0.0)
    offset: list[float] = Field(default_factory=lambda: [0.0, 0.0])
    # Effects rack (§7.4a): a convention an archetype bakes in by default —
    # the direction call may still override `treatment` per concept (a
    # non-"none" ArtPrompt.treatment wins; see model.build_spec), but
    # mask_from/corners/scatter are never the model's to set, only the
    # archetype's (or a later revision's).
    treatment: Literal["none", "duotone", "silhouette", "posterize",
                       "sticker", "photo_soft"] = "none"
    mask_from: str = ""
    # First-class mask (§15.2, archetype-authored — the §15.13 mask-forward
    # templates' enabler): mirrors docproof.cover.model.ArtSlot.mask exactly;
    # build_spec passes it straight into a real MaskSpec. `mask_from` above
    # stays as the legacy single-stencil sugar — setting BOTH is refused
    # below (same "two masks with an undocumented winner" reasoning as
    # ArtSlot's own fold validator), so an archetype always says the thing
    # it means exactly once.
    mask: ArchetypeMask | None = None
    corners: bool = False
    # Mirrors docproof.cover.model.ArtSlot.corners_flip_vertical exactly
    # (v2.2 wave, deliverable 1): False keeps all four corners-mirrored
    # copies upright by default; True restores the original full-mirror
    # (bottom copies also vertically flipped) for a genuinely symmetric
    # ornament that wants it.
    corners_flip_vertical: bool = False
    scatter: int = Field(default=0)
    # Mirrors docproof.cover.model.ArtSlot.snap exactly (v2.2 wave,
    # deliverable 3): "" = off, "line_gap" snaps a contain-fit slot drawn
    # immediately after a text layer into that text's own largest
    # inter-line gap instead of a fixed anchor point.
    snap: Literal["", "line_gap"] = ""
    # Mirrors docproof.cover.model.ArtSlot.texture_file/texture_fit exactly
    # (v2.2 wave, deliverable 5): names a docproof.cover.textures.TEXTURES
    # shelf plate to draw when this slot has no generated asset.
    texture_file: str = ""
    texture_fit: Literal["tile", "cover"] = "cover"
    # Mirrors docproof.cover.model.ArtSlot.notch_for exactly (v2.2 wave,
    # deliverable 7): another art slot in this SAME archetype whose
    # positioned bbox gets erased from this slot's own painted pixels.
    notch_for: str = ""
    # Mirrors docproof.cover.model.ArtSlot.procedural exactly (same twelve
    # names — the original seven plus the v2.2 wave's five frame-family
    # entries — same "" = no-opinion default that falls back to the
    # ORIGINAL hardcoded-by-id background/texture behavior — v2 BODY wave).
    procedural: Literal["", "gradient", "grain", "paper", "halftone",
                        "canvas", "speckle", "rule_frame", "frame_hairline",
                        "frame_thickthin", "frame_corners", "frame_deco",
                        "frame_octagon", "radial_glow", "light_leak",
                        "fog_gradient", "rays", "bokeh", "dust", "scratches",
                        "stars"] = ""
    # Mirrors docproof.cover.model.ArtSlot.effects exactly (§15.4): a
    # designed layer-style stack this archetype bakes onto the slot.
    effects: list[ArchetypeEffect] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        _validate_slot_id(value)
        if value.startswith(_FX_PREFIX):
            raise ValueError(
                f"{value!r}: the {_FX_PREFIX!r} prefix is reserved for "
                f"recipe-expanded finishing layers (§15.6) — a hand-authored "
                f"archetype slot may not use it")
        return value

    @field_validator("scatter")
    @classmethod
    def _scatter_bounds(cls, value: int) -> int:
        return _scatter_range(value)

    @field_validator("prompt_frame")
    @classmethod
    def _frame_has_subject(cls, value: str) -> str:
        """A frame without the hole is not a frame — it would silently
        discard whatever noun the direction supplied and generate the same
        plate for every book, which is exactly the failure this field
        exists to prevent."""
        if value and "{subject}" not in value:
            raise ValueError(
                "prompt_frame must contain the literal '{subject}' "
                "placeholder — that is where the art-direction call's noun "
                "goes")
        return value

    @field_validator("texture_file")
    @classmethod
    def _known_texture(cls, value: str) -> str:
        """Mirrors docproof.cover.model.ArtSlot._known_texture exactly —
        re-validated here so a malformed SHIPPED archetype fails at import
        (this module's own "fails LOUDLY" philosophy — see the module
        docstring), not only the first time a real brief builds a spec
        from it."""
        if value and value not in TEXTURES:
            raise ValueError(
                f"texture_file {value!r} is not on the shelf — known "
                f"textures: {', '.join(sorted(TEXTURES)) or 'none'}")
        return value

    @field_validator("anchor", "offset")
    @classmethod
    def _pair(cls, value: list[float]) -> list[float]:
        """Mirrors ArtSlot._pair in model.py exactly. Without this, a
        malformed YAML pair loads fine here and only explodes later inside
        build_spec — in a detached job task, where the module's own "fails
        LOUDLY at import" promise is worth nothing."""
        if len(value) != 2:
            raise ValueError("anchor/offset must be exactly [x, y]")
        if not all(-2.0 <= v <= 2.0 for v in value):
            raise ValueError("anchor/offset values must stay within [-2, 2]")
        return value

    @model_validator(mode="after")
    def _one_mask_vocabulary(self) -> ArchetypeArt:
        """`mask_from` is the legacy single-stencil sugar; `mask` is the
        full §15.2 vocabulary. Both set at once is refused — stricter than
        ArtSlot's own fold validator (which tolerates the exact folded
        equivalent for round-trip reasons a YAML file never has): a
        hand-authored template should say the thing it means exactly once,
        in one field."""
        if self.mask is not None and self.mask_from:
            raise ValueError(
                f"art slot {self.id!r} sets both mask_from and mask — use "
                f"mask.from_layer (mask wins the vocabulary) and drop "
                f"mask_from")
        return self


class ArchetypeAdjust(BaseModel):
    """One adjustment layer a template bakes in — mirrors
    docproof.cover.model.AdjustLayer field for field, defaults included, and
    build_spec passes each straight into a real AdjustLayer.

    These are the six-to-ten adjustment layers of a real cover PSD: the
    grade that quiets a generated ground, the masked haze that pushes a far
    tier back, the gradient map that puts a dozen separately-lit plates on
    one tonal spine, the feathered wash that makes a foot legible. Before
    this model existed a template could not declare a single one of them —
    only the closed `recipes` shelf could emit adjust layers — so any
    template whose look genuinely depended on them (which is any template
    that assembles more than three plates) had to be finished by hand after
    the spec was built, outside the template, where no art-direction call
    could reach it.

    Per-op parameters are FLAT fields, exactly as on AdjustLayer, and
    fields the chosen `op` never reads are validated but inert."""
    model_config = ConfigDict(extra="forbid")

    id: str
    op: Literal["grade", "gradient_map", "color_wash", "vignette", "bloom",
                "blur"]
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    blend: Literal["normal", "multiply", "overlay", "soft_light", "screen",
                   "add", "lighten", "darken", "color_dodge"] = "normal"
    mask: ArchetypeMask | None = None
    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)
    contrast: float = Field(default=0.0, ge=-1.0, le=1.0)
    saturation: float = Field(default=0.0, ge=-1.0, le=1.0)
    temperature: float = Field(default=0.0, ge=-1.0, le=1.0)
    stops: list[str] = Field(default_factory=list)
    color: str = ""
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    radius: float = Field(default=0.02, ge=0.0, le=0.25)
    threshold: float = Field(default=0.75, ge=0.0, le=1.0)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        _validate_slot_id(value)
        if value.startswith(_FX_PREFIX):
            raise ValueError(
                f"{value!r}: the {_FX_PREFIX!r} prefix is reserved for "
                f"recipe-expanded finishing layers (§15.6) — a hand-authored "
                f"archetype adjust layer may not use it")
        return value

    @field_validator("stops")
    @classmethod
    def _valid_stops(cls, value: list[str]) -> list[str]:
        if len(value) not in (0, 2, 3):
            raise ValueError(
                f"stops must have 2 or 3 entries (or be empty when unused), "
                f"got {len(value)}")
        return [_role_or_hex(stop) for stop in value]

    @field_validator("color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        return _role_or_hex(value) if value else value

    @model_validator(mode="after")
    def _op_requirements(self) -> ArchetypeAdjust:
        """Mirrors AdjustLayer._op_requirements: a gradient_map with no
        stops has no ramp to map through, so it fails at LOAD time rather
        than rendering something invented."""
        if self.op == "gradient_map" and not self.stops:
            raise ValueError(
                f"adjust layer {self.id!r} is a gradient_map with no stops "
                f"— give it 2 or 3 (role names or #rrggbb hexes, dark to "
                f"light)")
        return self


class ArchetypeScrim(BaseModel):
    """One default scrim. `zone` is deliberately absent here (as in the
    runtime ScrimSpec, absent means "derived from the protected TextSlot") —
    no launch archetype needs a scrim untied from a text slot, so the field
    is not offered rather than offered-and-unused."""
    model_config = ConfigDict(extra="forbid")

    # Mirrors docproof.cover.model.ScrimSpec.kind exactly — "halo" (v2.2
    # wave, deliverable 2) added alongside the original four.
    kind: Literal["gradient_down", "gradient_up", "vignette", "panel", "halo"] = "panel"
    protects: Literal["title", "subtitle", "author", "series"] | None = None
    strength: float = Field(default=0.0, ge=0.0, le=1.0)


class ArchetypeText(BaseModel):
    """One text slot's fitting rules. No `font_family`, `content`, or
    `color_role`: those come from the Direction (font), the Brief (content),
    and TextSlot's own default (color_role), never from the archetype
    template — see docproof.cover.model.build_spec."""
    model_config = ConfigDict(extra="forbid")

    id: Literal["title", "subtitle", "author", "series"]
    # Which of the direction's TWO fonts this slot wears. "" — the default,
    # and every template that predates the field — keeps the original rule:
    # the title slot gets title_font, every other slot gets author_font.
    # Naming a role overrides that, which is the only way a template can put
    # the author's name in the display face while the tagline and the credit
    # line stay in the supporting one — a convention half the shelf uses and
    # none of them could express.
    font_role: Literal["", "title", "author"] = ""
    zone: ArchetypeZone
    case: Literal["upper", "title", "as_is"] = "as_is"
    tracking: float = 0.0
    align: Literal["left", "center", "right"] = "center"
    valign: Literal["top", "middle", "bottom"] = "middle"
    max_lines: int = Field(default=3, ge=1)
    size_min: float = Field(gt=0.0)
    size_max: float = Field(gt=0.0)
    optional: bool = False
    shadow: ArchetypeShadow | None = None
    stroke: ArchetypeStroke | None = None
    # Mirrors docproof.cover.model.TextSlot.effects exactly (§15.4): a
    # designed layer-style stack for this slot (the thriller retrofit's
    # stacked double title shadow). The runtime fold — legacy shadow to the
    # stack's front, stroke to its back when this list is non-empty —
    # happens in TextSlot's own validator once build_spec converts these,
    # never here.
    effects: list[ArchetypeEffect] = Field(default_factory=list)
    # "fill" is the launch default everywhere; knockout/art_fill (§7.4a) are
    # archetype/revision territory — no Direction field sets this, so the
    # only way a concept ever gets one is an archetype that presets it.
    mode: Literal["fill", "knockout", "art_fill"] = "fill"
    # "thing inside of thing" (v2 BODY wave): mirrors
    # docproof.cover.model.TextSlot.mask_from exactly — an art slot id this
    # text is clipped to, or "" = off. Checked for existence (never
    # ordering — see Archetype._text_mask_from_exists below) at load time.
    mask_from: str = ""

    @model_validator(mode="after")
    def _size_range(self) -> ArchetypeText:
        if self.size_min > self.size_max:
            raise ValueError(
                f"{self.id}: size_min ({self.size_min}) exceeds size_max "
                f"({self.size_max})")
        return self


class Archetype(BaseModel):
    """A whole layout template, as loaded from one config/cover/archetypes/
    *.yaml file. `layers` is bottom-first z-order, addressed by the compact
    string form the YAML uses (an art or text slot id, or "scrim:N" for the
    Nth entry of `scrims`) — build_spec expands each into a structured
    LayerRef. See docs/cover_designer_spec.md §5.1 for the shape this
    mirrors.

    `genres` (§5.3) tags which of the ten SUBJECT_KEYS this template's
    convention was researched for — describe_archetypes() uses it to narrow
    the art-direction call's enumerated choices to what actually fits the
    brief. Empty (the default, and the three launch archetypes' permanent
    state — they are deliberately never edited to add one) means "fits every
    genre": an untagged archetype is always in scope, filtered or not."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    describe: str = Field(min_length=1)
    composition_note: str = Field(min_length=1)
    art: list[ArchetypeArt] = Field(min_length=1)
    # Adjustment layers this template bakes in (see ArchetypeAdjust).
    # Defaulted empty so every template that predates the field validates
    # and renders byte-identically.
    adjust: list[ArchetypeAdjust] = Field(default_factory=list)
    scrims: list[ArchetypeScrim] = Field(default_factory=list)
    text: list[ArchetypeText] = Field(min_length=1)
    layers: list[str] = Field(min_length=1)
    genres: list[str] = Field(default_factory=list)
    # Mirrors docproof.cover.model.CoverSpec.axis/axis_x exactly (§15.10;
    # build_spec copies both verbatim): which vertical axis this template
    # composes around, so the balance engine's snap pass knows what a
    # near-miss is a near-miss OF. None — the default, and every shipped
    # archetype's current state until a later wave retrofits declarations
    # — means pre-wave behavior: no snap pass, byte-identical renders
    # (§15.0 constraint 2). axis_x (fraction of canvas width) positions a
    # left/right rail; None takes §15.10's conventional 0.08/0.92, and a
    # center axis never reads it at all.
    axis: Literal["center", "left", "right"] | None = None
    axis_x: float | None = Field(default=None, ge=0.0, le=1.0)
    # The finishing recipe this template wears BY DEFAULT (§15.6) — applied
    # by build_spec whenever the direction stays silent (recipe=""); a
    # direction's own non-"" pick always wins. "" (the default, and every
    # un-retrofitted archetype's permanent state) means no default
    # finishing at all: the no-recipe path renders byte-identical to
    # pre-wave pixels, which is §15.0 constraint 2 for this field.
    recipe: str = ""
    # Scales every value the chosen recipe's own finishing layers carry —
    # adjust strengths and art-layer opacities alike. 1.0 (the default, and
    # every template that predates the field) applies the shelf recipe
    # exactly as written. A shelf recipe is tuned for the kind of cover it
    # was named for, and a template that assembles a dozen separately-lit
    # plates usually wants a fraction of it: dark_academia at full strength
    # cooks such a collage into one sepia mass, while at 0.3 it still
    # supplies the vignette, dust and paper tooth that make the thing feel
    # printed. Dialling the shelf entry itself is not an option — other
    # templates share it.
    recipe_strength: float = Field(default=1.0, ge=0.0, le=1.0)
    # Whether this template's plates are PHOTOGRAPHIC by construction, and
    # may therefore be prompted photoreal with `treatment: "none"` —
    # the one exemption from direction.py's shelf-wide ban on untreated
    # photorealism (docs/cover_designer_spec.md §18.4).
    #
    # That ban is not squeamishness: a raw, untreated photoreal plate is the
    # single biggest "AI-generated" tell, and the shelf's mitigation is
    # stylization — silhouette, duotone, posterize, or the photo_soft
    # blur+desaturate+ramp. A photoreal TEMPLATE cannot use any of them.
    # photo_soft is a duotone: it greyscales each plate and maps it onto the
    # background->primary ramp, which across eight separately-lit plates
    # flattens a whole cover into one sepia mass — the same reason
    # romantasy_organic forbids it outright.
    #
    # So this flag is not "this archetype likes photographs". It ASSERTS that
    # the template carries its own photoreal discipline in place of the
    # stylization it cannot use: one `composition_note` fixing the medium,
    # the key, the fill and the saturation for every plate identically, and a
    # finishing `recipe` (grade + bloom + grain) unifying them afterward.
    # Those are what make eight separate generations read as one photograph
    # rather than as eight stock images in a pile, and the validator below
    # holds the template to the second half of that bargain.
    photoreal: bool = False

    @model_validator(mode="after")
    def _photoreal_has_a_finish(self) -> Archetype:
        """A photoreal template must declare a finishing `recipe`. The grade,
        the bloom and above all the GRAIN are what put eight separately
        generated plates on one piece of film; without them the exemption is
        just permission to ship untreated stock photography, which is the
        thing the shelf-wide rule exists to prevent."""
        if self.photoreal and not self.recipe:
            raise ValueError(
                f"{self.name}: photoreal: true requires a finishing `recipe` "
                f"— it is the grade/bloom/grain that unifies separately "
                f"generated photographic plates, and it is half of what the "
                f"flag asserts (known recipes: "
                f"{', '.join(sorted(RECIPES)) or 'none'})")
        return self

    @field_validator("recipe")
    @classmethod
    def _known_recipe(cls, value: str) -> str:
        """The archetype-side twin of Direction.recipe's closed Literal —
        checked against the same recipes.RECIPES shelf at load time, so a
        typo'd default fails the build loudly (this module's whole "core
        inventory" philosophy) instead of expanding to nothing the first
        time a brief picks the template."""
        if value and value not in RECIPES:
            raise ValueError(
                f"recipe {value!r} is not on the shelf — known recipes: "
                f"{', '.join(sorted(RECIPES)) or 'none'}")
        return value

    @model_validator(mode="after")
    def _unique_ids(self) -> Archetype:
        art_ids = [a.id for a in self.art]
        if len(set(art_ids)) != len(art_ids):
            raise ValueError(f"{self.name}: duplicate art slot id in {art_ids}")
        text_ids = [t.id for t in self.text]
        if len(set(text_ids)) != len(text_ids):
            raise ValueError(f"{self.name}: duplicate text slot id in {text_ids}")
        # Adjust layers share the art-slot id namespace, exactly as they do
        # on CoverSpec (§15.3), so a `layers` entry naming one is never
        # ambiguous about what it means.
        adjust_ids = [a.id for a in self.adjust]
        if len(set(adjust_ids)) != len(adjust_ids):
            raise ValueError(
                f"{self.name}: duplicate adjust layer id in {adjust_ids}")
        clash = sorted(set(adjust_ids) & set(art_ids))
        if clash:
            raise ValueError(
                f"{self.name}: adjust layer id(s) {clash} collide with an "
                f"art slot of the same id — the two kinds share one "
                f"namespace")
        return self

    @model_validator(mode="after")
    def _known_genres(self) -> Archetype:
        """A typo'd genre tag ('fantasy_' or 'YA') must fail loudly at load —
        the same "core inventory, not an optional extra" philosophy as the
        rest of this module's validation (see the module docstring) — rather
        than silently never matching any brief's genre filter."""
        bad = [g for g in self.genres if g not in SUBJECT_KEYS]
        if bad:
            raise ValueError(
                f"{self.name}: genres {bad!r} not in the ten subject keys "
                f"({', '.join(sorted(SUBJECT_KEYS))})")
        return self

    @model_validator(mode="after")
    def _layers_resolve(self) -> Archetype:
        """Every layers entry must name something real: an art slot id, a
        text slot id, or scrim:N for a valid index into `scrims` — checked
        here, at load time, so a typo'd layer reference fails loudly instead
        of silently never drawing."""
        art_ids = {a.id for a in self.art}
        text_ids = {t.id for t in self.text}
        adjust_ids = {a.id for a in self.adjust}
        n_scrims = len(self.scrims)
        for ref in self.layers:
            if ref.startswith("scrim:"):
                idx = ref.removeprefix("scrim:")
                if not idx.isdigit() or int(idx) >= n_scrims:
                    raise ValueError(
                        f"{self.name}: layers entry {ref!r} does not resolve "
                        f"— only {n_scrims} scrim(s) defined")
            elif (ref not in art_ids and ref not in text_ids
                  and ref not in adjust_ids):
                raise ValueError(
                    f"{self.name}: layers entry {ref!r} matches no art slot, "
                    f"adjust layer, text slot, or scrim index")
        # Every declared adjust layer must actually be drawn: an adjust
        # layer absent from `layers` is silently inert, which is exactly the
        # kind of three-steps-later surprise this module validates against.
        drawn = set(self.layers)
        orphans = sorted(adjust_ids - drawn)
        if orphans:
            raise ValueError(
                f"{self.name}: adjust layer(s) {orphans} are declared but "
                f"never appear in `layers`, so they would never be drawn")
        return self

    @model_validator(mode="after")
    def _adjust_masks_resolve(self) -> Archetype:
        """The adjust-layer twin of _art_masks_resolve: an adjust layer's
        mask obeys the same rules an art slot's does — `from_layer` must
        name a real art slot drawn EARLIER in `layers` (a mask can only clip
        to pixels already positioned), `luminance_of`/`from_text` need
        existence only."""
        art_ids = {a.id for a in self.art}
        text_ids = {t.id for t in self.text}
        first_position: dict[str, int] = {}
        for i, ref in enumerate(self.layers):
            if ref not in first_position:
                first_position[ref] = i
        for layer in self.adjust:
            mask = layer.mask
            if mask is None:
                continue
            if mask.from_layer:
                if mask.from_layer not in art_ids:
                    raise ValueError(
                        f"{self.name}: adjust layer {layer.id!r} has "
                        f"mask.from_layer={mask.from_layer!r}, which is not "
                        f"one of this archetype's art slots "
                        f"({', '.join(sorted(art_ids))})")
                this_pos = first_position.get(layer.id)
                ref_pos = first_position.get(mask.from_layer)
                if this_pos is None or ref_pos is None or ref_pos >= this_pos:
                    raise ValueError(
                        f"{self.name}: adjust layer {layer.id!r}'s mask."
                        f"from_layer={mask.from_layer!r} must appear earlier "
                        f"in `layers` than {layer.id!r} itself")
            if mask.luminance_of and mask.luminance_of not in art_ids:
                raise ValueError(
                    f"{self.name}: adjust layer {layer.id!r} has "
                    f"mask.luminance_of={mask.luminance_of!r}, which is not "
                    f"one of this archetype's art slots "
                    f"({', '.join(sorted(art_ids))})")
            if mask.from_text and mask.from_text not in text_ids:
                raise ValueError(
                    f"{self.name}: adjust layer {layer.id!r} has "
                    f"mask.from_text={mask.from_text!r}, which is not one of "
                    f"this archetype's text slots "
                    f"({', '.join(sorted(text_ids))})")
        return self

    @model_validator(mode="after")
    def _mask_from_precedes(self) -> Archetype:
        """Mirrors docproof.cover.model.CoverSpec's own _mask_from_precedes
        exactly (same rule: a mask_from target must exist and come earlier
        in `layers`), checked again here so a malformed SHIPPED archetype
        fails at import — this module's whole "fails LOUDLY" philosophy
        (see the module docstring) — rather than only surfacing the first
        time a real brief happens to build a spec from it."""
        art_ids = {a.id for a in self.art}
        first_position: dict[str, int] = {}
        for i, ref in enumerate(self.layers):
            if ref in art_ids and ref not in first_position:
                first_position[ref] = i
        for slot in self.art:
            if not slot.mask_from:
                continue
            if slot.mask_from not in art_ids:
                raise ValueError(
                    f"{self.name}: art slot {slot.id!r} has mask_from="
                    f"{slot.mask_from!r}, which is not one of this "
                    f"archetype's art slots ({', '.join(sorted(art_ids))})")
            this_pos = first_position.get(slot.id)
            ref_pos = first_position.get(slot.mask_from)
            if this_pos is None or ref_pos is None or ref_pos >= this_pos:
                raise ValueError(
                    f"{self.name}: art slot {slot.id!r}'s mask_from="
                    f"{slot.mask_from!r} must appear earlier in `layers` "
                    f"than {slot.id!r} itself")
        return self

    @model_validator(mode="after")
    def _art_masks_resolve(self) -> Archetype:
        """The archetype-side twin of CoverSpec._masks_resolve, for the new
        first-class `mask` field (§15.13's mask-forward templates), checked
        at load so a malformed SHIPPED template fails at import: `from_layer`
        must name a real art slot that appears earlier in `layers` (the
        exact _mask_from_precedes rule); `luminance_of` and `from_text` need
        existence only (order-free sources, per CoverSpec's own reasoning);
        and `from_text` refuses the one true cycle — clipping into a text
        slot that is itself mask_from-clipped to this same art slot."""
        art_ids = {a.id for a in self.art}
        text_by_id = {t.id: t for t in self.text}
        first_position: dict[str, int] = {}
        for i, ref in enumerate(self.layers):
            if ref in art_ids and ref not in first_position:
                first_position[ref] = i
        for slot in self.art:
            mask = slot.mask
            if mask is None:
                continue
            if mask.from_layer:
                if mask.from_layer not in art_ids:
                    raise ValueError(
                        f"{self.name}: art slot {slot.id!r} has "
                        f"mask.from_layer={mask.from_layer!r}, which is not "
                        f"one of this archetype's art slots "
                        f"({', '.join(sorted(art_ids))})")
                this_pos = first_position.get(slot.id)
                ref_pos = first_position.get(mask.from_layer)
                if this_pos is None or ref_pos is None or ref_pos >= this_pos:
                    raise ValueError(
                        f"{self.name}: art slot {slot.id!r}'s mask."
                        f"from_layer={mask.from_layer!r} must appear earlier "
                        f"in `layers` than {slot.id!r} itself")
            if mask.luminance_of and mask.luminance_of not in art_ids:
                raise ValueError(
                    f"{self.name}: art slot {slot.id!r} has "
                    f"mask.luminance_of={mask.luminance_of!r}, which is not "
                    f"one of this archetype's art slots "
                    f"({', '.join(sorted(art_ids))})")
            if mask.from_text:
                target = text_by_id.get(mask.from_text)
                if target is None:
                    raise ValueError(
                        f"{self.name}: art slot {slot.id!r} has "
                        f"mask.from_text={mask.from_text!r}, which is not "
                        f"one of this archetype's text slots "
                        f"({', '.join(sorted(text_by_id))})")
                if target.mask_from == slot.id:
                    raise ValueError(
                        f"{self.name}: art slot {slot.id!r} clips into text "
                        f"slot {mask.from_text!r}'s glyphs while that text "
                        f"slot is itself clipped to {slot.id!r} — a cycle")
        return self

    @model_validator(mode="after")
    def _text_mask_from_exists(self) -> Archetype:
        """Mirrors docproof.cover.model.CoverSpec's own
        _text_mask_from_resolves — existence only, deliberately no
        "precedes" requirement (see that method's docstring for why draw
        order doesn't matter for a text-clipped-to-art container)."""
        art_ids = {a.id for a in self.art}
        for slot in self.text:
            if slot.mask_from and slot.mask_from not in art_ids:
                raise ValueError(
                    f"{self.name}: text slot {slot.id!r} has mask_from="
                    f"{slot.mask_from!r}, which is not one of this "
                    f"archetype's art slots ({', '.join(sorted(art_ids))})")
        return self

    @model_validator(mode="after")
    def _notch_for_exists(self) -> Archetype:
        """Mirrors docproof.cover.model.CoverSpec's own
        _notch_for_resolves (v2.2 wave, deliverable 7) — existence and
        not-self-reference only, no "precedes" requirement, for the same
        reason _text_mask_from_exists has none: the notch is applied as a
        finishing pass once every art slot in an archetype is already
        positioned, so draw order between a frame and its notch_for target
        never matters."""
        art_ids = {a.id for a in self.art}
        for slot in self.art:
            if not slot.notch_for:
                continue
            if slot.notch_for == slot.id:
                raise ValueError(
                    f"{self.name}: art slot {slot.id!r} cannot set "
                    f"notch_for to itself")
            if slot.notch_for not in art_ids:
                raise ValueError(
                    f"{self.name}: art slot {slot.id!r} has notch_for="
                    f"{slot.notch_for!r}, which is not one of this "
                    f"archetype's art slots ({', '.join(sorted(art_ids))})")
        return self


class _Fractional(Protocol):
    """Anything shaped like a fractional zone — ArchetypeZone here, or
    docproof.cover.model.Zone from the caller's side. Structural typing
    means this module never has to import model.py to accept one."""
    x: float
    y: float
    w: float
    h: float


def zone_px(zone: _Fractional, canvas: tuple[int, int]) -> tuple[int, int, int, int]:
    """A fractional zone at one canvas size, in pixels: (left, top, width,
    height). `canvas` is (width, height), matching compose.py's own
    `canvas=(1600, 2560)` convention. The one place zone fractions become
    pixels, so the fit search and scrim math never re-derive it."""
    canvas_w, canvas_h = canvas
    left = round(zone.x * canvas_w)
    top = round(zone.y * canvas_h)
    width = round(zone.w * canvas_w)
    height = round(zone.h * canvas_h)
    return left, top, width, height


def _load_one(path: Path) -> Archetype:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ArchetypeError(f"{path}: not readable YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ArchetypeError(
            f"{path}: root must be a YAML mapping, not {type(raw).__name__}")
    try:
        archetype = Archetype.model_validate(raw)
    except ValidationError as e:
        raise ArchetypeError(f"{path}: does not validate: {e}") from e
    if archetype.name != path.stem:
        raise ArchetypeError(
            f"{path}: name {archetype.name!r} does not match the file name "
            f"{path.stem!r}")
    return archetype


def load_archetypes(archetypes_dir: str | Path | None = None
                    ) -> dict[str, Archetype]:
    """Every `*.yaml` archetype in `archetypes_dir` (package-relative by
    default), loaded and validated. `archetypes_dir` exists mainly so a test
    can point this at a tmp_path fixture — see docproof/genre.py's
    `genres_dir` / docproof/stages.py's `stages_dir` for the same pattern."""
    root = Path(archetypes_dir) if archetypes_dir else ARCHETYPES_DIR
    if not root.is_dir():
        raise ArchetypeError(f"No archetype directory at {root}")
    out: dict[str, Archetype] = {}
    for path in sorted(root.glob("*.yaml")):
        out[path.stem] = _load_one(path)
    if not out:
        raise ArchetypeError(f"No archetype files found in {root}")
    return out


# Loaded once, at import — a malformed shipped archetype should break the
# build immediately, not surface as a mystery 500 the first time a brief
# happens to pick it. describe_archetypes() reads this dict, never the disk.
ARCHETYPES: dict[str, Archetype] = load_archetypes()


def _fits_genre(archetype: Archetype, genre: str) -> bool:
    """An archetype is in scope for `genre` when it names that genre
    explicitly, or names none at all — an untagged archetype (the three
    launch ones, permanently) always fits, tagged or not (§5.3)."""
    return not archetype.genres or genre in archetype.genres


def describe_archetypes(genre: str | None = None) -> str:
    """The archetypes as prompt text — name + describe line each — the same
    shape as docproof.prep.book_design.BookDesign.describe_subjects(), for
    the art-direction call (docproof.cover.direction) to enumerate and pick
    from.

    `genre` narrows the list to archetypes whose `genres` contains it, PLUS
    every untagged archetype (§5.3). `genre=None` — the default, so the
    zero-arg call every existing caller already makes keeps working
    unchanged — and any `genre` that is not one of the ten SUBJECT_KEYS both
    mean "no filter": every archetype is described, exactly like before this
    field existed. docproof.cover.direction.run_directions is the one real
    caller and does its own exact-match normalization against SUBJECT_KEYS
    before calling this (so a free-text brief genre never reaches here as
    anything but None) — the same fallback is repeated here, rather than
    trusted to every caller, so this function is correct on its own even if
    called with an un-normalized string directly (as these tests do)."""
    if genre in SUBJECT_KEYS:
        archetypes = [a for a in ARCHETYPES.values() if _fits_genre(a, genre)]
    else:
        archetypes = list(ARCHETYPES.values())
    lines = []
    for a in archetypes:
        gen = [s for s in a.art if s.generatable]
        # The slot ids are load-bearing prompt content: v2's free-form slugs
        # mean the art director can no longer guess them, and a prompt for a
        # misspelled slot is silently dropped downstream — the first live v2
        # batch shipped every cover artless for exactly this reason.
        #
        # And the id ALONE is not enough. An id is a label, not a brief: told
        # only that a template wants "luminary" and "token_near", a model
        # fills them from the nearest cover it can remember rather than from
        # the book in front of it — which is how a template stops being a
        # template and becomes an impression of the one cover it was drawn
        # from. `role` is the slot's own statement of what it is FOR, it has
        # existed on ArchetypeArt since the v2 BODY wave, and until this it
        # reached no prompt anywhere: it was documentation the only audience
        # that needed it never saw. Emitting it here is what lets a template
        # ask for "the one big light source in this book's sky" instead of
        # hoping "luminary" means the same thing to the model as it did to
        # whoever wrote the YAML.
        pairs = [f"{s.id} — {' '.join(s.role.split())}" if s.role else s.id
                 for s in gen]
        slots = (f" (art slots to prompt, by exact id: {'; '.join(pairs)})"
                 if gen else " (no generated art — fully procedural)")
        # A photoreal template is the ONE exemption from the shelf-wide ban
        # on untreated photorealism, and the exemption is worthless if the
        # call cannot tell which templates hold it: told only "never ship an
        # untreated photoreal prompt", a model picking this archetype either
        # refuses its own plates or pairs them with photo_soft, which is a
        # duotone and destroys the template. Marked here, at the point of
        # choice, rather than left to be inferred from the describe line.
        mark = (" [PHOTOREAL TEMPLATE — prompt these plates photographically "
                "and leave treatment \"none\"; see the photorealism rule]"
                if a.photoreal else "")
        lines.append(f"- {a.name} — {' '.join(a.describe.split())}{mark}{slots}")
    return "\n".join(lines)


__all__ = ["ARCHETYPES", "ARCHETYPES_DIR", "SUBJECT_KEYS", "Archetype",
          "ArchetypeAdjust",
          "ArchetypeArt", "ArchetypeEffect", "ArchetypeError",
          "ArchetypeGradientMask", "ArchetypeMask",
          "ArchetypeScrim", "ArchetypeShadow", "ArchetypeStroke",
          "ArchetypeText", "ArchetypeZone", "describe_archetypes",
          "load_archetypes", "zone_px"]
