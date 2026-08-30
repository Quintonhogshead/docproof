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
    fit: Literal["cover", "contain"] = "cover"
    transparent: bool = False
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    blend: Literal["normal", "multiply", "overlay", "soft_light"] = "normal"
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
                        "frame_octagon"] = ""

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_slot_id(value)

    @field_validator("scatter")
    @classmethod
    def _scatter_bounds(cls, value: int) -> int:
        return _scatter_range(value)

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

    @model_validator(mode="after")
    def _unique_ids(self) -> Archetype:
        art_ids = [a.id for a in self.art]
        if len(set(art_ids)) != len(art_ids):
            raise ValueError(f"{self.name}: duplicate art slot id in {art_ids}")
        text_ids = [t.id for t in self.text]
        if len(set(text_ids)) != len(text_ids):
            raise ValueError(f"{self.name}: duplicate text slot id in {text_ids}")
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
        n_scrims = len(self.scrims)
        for ref in self.layers:
            if ref.startswith("scrim:"):
                idx = ref.removeprefix("scrim:")
                if not idx.isdigit() or int(idx) >= n_scrims:
                    raise ValueError(
                        f"{self.name}: layers entry {ref!r} does not resolve "
                        f"— only {n_scrims} scrim(s) defined")
            elif ref not in art_ids and ref not in text_ids:
                raise ValueError(
                    f"{self.name}: layers entry {ref!r} matches no art slot, "
                    f"text slot, or scrim index")
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
        gen = [s.id for s in a.art if s.generatable]
        # The slot ids are load-bearing prompt content: v2's free-form slugs
        # mean the art director can no longer guess them, and a prompt for a
        # misspelled slot is silently dropped downstream — the first live v2
        # batch shipped every cover artless for exactly this reason.
        slots = (f" (art slots to prompt, by exact id: {', '.join(gen)})"
                 if gen else " (no generated art — fully procedural)")
        lines.append(f"- {a.name} — {' '.join(a.describe.split())}{slots}")
    return "\n".join(lines)


__all__ = ["ARCHETYPES", "ARCHETYPES_DIR", "SUBJECT_KEYS", "Archetype",
          "ArchetypeArt", "ArchetypeError", "ArchetypeScrim",
          "ArchetypeShadow", "ArchetypeStroke", "ArchetypeText",
          "ArchetypeZone", "describe_archetypes", "load_archetypes",
          "zone_px"]
