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

import logging
import re
from enum import Enum
from typing import Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, create_model,
                      field_validator, model_validator)

from .archetypes import Archetype
from .fonts import FAMILIES
from .recipes import RECIPES
from .textures import TEXTURES

log = logging.getLogger("docproof.cover.model")

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

# Every blend mode a pixel-owning or adjust layer may name (deep-stack wave,
# §15.1) — the wire source of truth effects.BLEND_TABLE keys itself against
# (a unit test holds the two in lockstep, the ART_TREATMENTS/compose
# contract's shape). "normal" is plain alpha-over; multiply/overlay/
# soft_light are the pre-wave trio; screen/add/lighten/darken are one-line
# ImageChops calls and color_dodge runs per band through ImageMath.
# hue/color/luminosity are DEFERRED (§15.1): per-pixel HSL math is where
# pure Pillow gets ugly, and nothing in the finishing-recipe roster needs
# them — widen this tuple (and both Literals spelling it) only when a real
# cover demonstrably does.
BLEND_MODES: tuple[str, ...] = ("normal", "multiply", "overlay", "soft_light",
                                "screen", "add", "lighten", "darken",
                                "color_dodge")


def _validate_role_or_hex(value: str) -> str:
    """A color reference that may be either a PaletteRole name (so the value
    tracks palette revisions for free) or a literal #rrggbb hex (for the one
    stop a ramp needs outside the five roles) — the deep-stack wave's
    gradient_map/color_wash vocabulary (§15.3). Anything else fails at spec
    time with both legal shapes named."""
    if value in {role.value for role in PaletteRole}:
        return value
    if _HEX_RE.match(value):
        return value
    raise ValueError(
        f"{value!r} is neither a palette role "
        f"({', '.join(role.value for role in PaletteRole)}) nor a #rrggbb "
        f"hex color")


# The paired token placements a Direction may pick from (§19.2). A
# one-shot template fixes placement so no book pays for it twice — the right
# trade for ONE cover, and a sameness generator across a catalogue: every
# book off portrait_luminary put its two accent plates at exactly the same
# two anchors, which is what made two covers with nothing else in common
# still rhyme. This is the smallest fix that keeps the bargain: the template
# still owns scale, opacity, effects and z-order, and the direction call gets
# ONE closed choice over where the pair sits.
#
# Every entry keeps the arrangement's two load-bearing properties — the two
# tokens are on OPPOSITE sides and at DIFFERENT heights — because that
# diagonal, not the tokens themselves, is what carries the eye across the
# face. A layout that stacked them would be a different composition, not a
# variation, so there isn't one on the shelf.
#
# Names read as "where the FAR (smaller, higher-up-the-stack) token sits";
# the near one always answers it from the opposite corner.
TOKEN_LAYOUTS: dict[str, dict[str, tuple[float, float]]] = {
    "far_high_left":  {"token_far": (0.10, 0.24), "token_near": (0.93, 0.50)},
    "far_high_right": {"token_far": (0.90, 0.24), "token_near": (0.07, 0.50)},
    "far_low_left":   {"token_far": (0.11, 0.60), "token_near": (0.90, 0.22)},
    "far_low_right":  {"token_far": (0.89, 0.60), "token_near": (0.10, 0.22)},
}

# The conventional slot ids a token layout addresses, mirroring how
# _intent_mask's "inside_focal" addresses the conventional `focal` id: an
# archetype that doesn't declare them simply isn't a template this choice
# applies to, and the pick is dropped with a log line rather than raising
# (the §6.1 surplus-prompt precedent).
TOKEN_SLOT_IDS: tuple[str, ...] = ("token_far", "token_near")


# The slot treatments every ArtSlot/ArtPrompt may request (§7.4a): pure,
# deterministic Pillow ops compose.py applies after fit/placement and before
# compositing. "none" is the default on every launch archetype and every
# archetype this session did not explicitly retrofit — the rack is opt-in,
# never a surprise on an existing cover. "photo_soft" (v2.2 wave) is the
# one treatment a photographic/photoreal art prompt may ever pair with —
# see direction.py's own photorealism doctrine.
ART_TREATMENTS: tuple[str, ...] = ("none", "duotone", "silhouette", "posterize",
                                   "sticker", "photo_soft")

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
    "gradient", "grain", "paper", "halftone", "canvas", "speckle", "rule_frame",
    # v2.2 wave, deliverable 7: the frame family — rule_frame's siblings, all
    # parameterized off the same inset constants (see compose._frame_inner_rect).
    "frame_hairline", "frame_thickthin", "frame_corners", "frame_deco",
    "frame_octagon",
    # Deep-stack wave, §15.5: the light & atmosphere bank — ordinary art
    # slots (usually screen/overlay/soft_light at low opacity), zero
    # per-synth params by design: anchor = center/origin/band-y, scale =
    # extent, opacity/blend as ever, inks derived from the palette only.
    "radial_glow", "light_leak", "fog_gradient", "rays", "bokeh", "dust",
    "scratches", "stars")


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
    # The credit/eyebrow line ("#1 New York Times Bestselling Author", "Book
    # Two of the Ashfall Cycle"). build_spec fills every text slot by
    # `getattr(brief, slot.id)`, so a `series` TextSlot had no source at all
    # until this field existed — an archetype could declare the slot and it
    # would always render empty.
    series: str = ""
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


class Effect(BaseModel):
    """One entry in a layer-style stack (deep-stack wave, §15.4): the
    ordered, repeatable generalization of "one Shadow + one Stroke, text
    only" to both TextSlot and ArtSlot. The SAME kind may repeat — a tight
    dark drop shadow under a wide soft one is the pro type move this model
    exists for.

    Paint-order semantics are FIXED, not per-effect: the engine
    (effects.apply_effect_stack) splits a stack into *under* effects
    (drop_shadow, outer_glow — painted beneath the layer's own pixels, in
    stack order) and *over* effects (inner_shadow, inner_glow, bevel,
    gradient_overlay, texture_overlay, stroke — applied against the layer's
    own alpha, after the fill, in stack order; stroke is the one over
    effect whose ring extends past that alpha, exactly like the legacy
    Pillow stroke it folds from).

    Params are FLAT fields, the AdjustLayer forgiving-fields rule verbatim
    (strict-schema wire: no dicts, no tuples; fields the chosen `kind`
    never reads are validated but inert, so a patch edit changing `kind`
    can never strand the spec). `color=""` means the kind-appropriate
    default, resolved at render time in effects.py — shadows #000000,
    glows the accent role — so the spec stays palette-tracking unless a
    hand says otherwise."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["drop_shadow", "inner_shadow", "outer_glow", "inner_glow",
                  "bevel", "gradient_overlay", "texture_overlay", "stroke"]
    # -- flat params, same forgiving-fields rule as AdjustLayer ---------------
    dx: float = 0.0                    # shadows: fraction of canvas HEIGHT
    dy: float = 0.004
    blur: float = Field(default=0.006, ge=0.0)       # shadows/glows
    color: str = ""                    # role or hex; "" = kind-appropriate
                                       # default (shadows #000000, glows the
                                       # accent role — see effects.py)
    alpha: float = Field(default=0.55, ge=0.0, le=1.0)
    width: float = Field(default=0.004, ge=0.0)      # stroke/bevel depth,
                                       # fraction of canvas height
    stops: list[str] = Field(default_factory=list)   # gradient_overlay:
                                       # 2-3 role-or-hex stops
    angle: float = 90.0                # gradient_overlay ramp direction
    texture_file: str = ""             # texture_overlay: a TEXTURES shelf
                                       # name (validated below)
    # §15.1's full table; read by the two overlays only.
    blend: Literal["normal", "multiply", "overlay", "soft_light", "screen",
                   "add", "lighten", "darken", "color_dodge"] = "normal"
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)   # overlays only

    @field_validator("color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        return _validate_role_or_hex(value) if value else value

    @field_validator("stops")
    @classmethod
    def _valid_stops(cls, value: list[str]) -> list[str]:
        """AdjustLayer._valid_stops' exact shape rule, for the exact same
        reason: 0 entries when unused, 2-3 when a ramp, each a role or
        hex — a single stop is a constant pretending to be a gradient."""
        if len(value) not in (0, 2, 3):
            raise ValueError(
                f"stops must have 2 or 3 entries (or be empty when unused), "
                f"got {len(value)}")
        return [_validate_role_or_hex(stop) for stop in value]

    @field_validator("texture_file")
    @classmethod
    def _known_texture(cls, value: str) -> str:
        """Mirrors ArtSlot._known_texture exactly — an unknown plate name
        fails loudly at spec time, never as a silently textureless title
        three steps later in the effects engine."""
        if value and value not in TEXTURES:
            raise ValueError(
                f"texture_file {value!r} is not on the shelf — known "
                f"textures: {', '.join(sorted(TEXTURES)) or 'none'}")
        return value

    @model_validator(mode="after")
    def _kind_requirements(self) -> Effect:
        """AdjustLayer._op_requirements' pattern: the two kinds with a
        REQUIRED input fail at spec time rather than rendering something
        invented — a gradient_overlay with no stops has no ramp, a
        texture_overlay with no plate has no texture. Every other kind's
        params have workable defaults."""
        if self.kind == "gradient_overlay" and not self.stops:
            raise ValueError(
                "a gradient_overlay effect needs 2 or 3 stops (role names "
                "or #rrggbb hexes, dark to light)")
        if self.kind == "texture_overlay" and not self.texture_file:
            raise ValueError(
                "a texture_overlay effect needs a texture_file from the "
                f"shelf ({', '.join(sorted(TEXTURES)) or 'none stocked'})")
        return self


def effect_from_shadow(shadow: Shadow) -> Effect:
    """The legacy Shadow spelled as a stack entry — §15.4's fold, in one
    place, so TextSlot's validator and compose's autopilot auto-Shadow
    prepend can never disagree about what a folded shadow looks like."""
    return Effect(kind="drop_shadow", dx=shadow.dx, dy=shadow.dy,
                  blur=shadow.blur, color=shadow.color, alpha=shadow.alpha)


def effect_from_stroke(stroke: Stroke) -> Effect:
    """The legacy Stroke as a stack entry. alpha=1.0 explicitly: the Pillow
    stroke this folds from is fully opaque, while Effect's own alpha
    default (0.55) is tuned for shadows."""
    return Effect(kind="stroke", width=stroke.width, color=stroke.color,
                  alpha=1.0)


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
    # The ordered layer-style stack (§15.4). Empty — every pre-wave spec —
    # means compose reads the legacy shadow/stroke fields EXACTLY as it
    # always has (byte-identical path); non-empty means _fold_shadow_stroke
    # below has already folded those two fields into this stack, and the
    # effects engine is the single code path that draws it.
    effects: list[Effect] = Field(default_factory=list)
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
    # -- expressive typography (§15.12) — the four type moves. All default
    # inert (every pre-wave spec renders byte-identical); all revision-
    # editable. The one-signature-move rule binds DIRECTIONS at build_spec
    # (PR6's vocabulary mapping), never this model: a hand-authored
    # archetype or a revision may legitimately combine moves.
    #
    # "uniform" is the launch fit search (one size for every line);
    # "justify_stack" sizes each line INDEPENDENTLY so its tracked width
    # fills the zone width exactly — the nonfiction/thriller poster stack.
    fit_mode: Literal["uniform", "justify_stack"] = "uniform"
    # Circular-baseline bow as a fraction of ZONE height (+ = arch/upward
    # bow, − = valley); glyphs place along the bowed baseline and rotate to
    # its local tangent. 0.0 = straight (the byte-identical legacy path).
    arc: float = Field(default=0.0, ge=-0.35, le=0.35)
    # Whole-slot tilt in degrees: rendered flat, then the finished text
    # layer rotates (expand) and re-anchors per align/valign in the zone,
    # clamped inside the canvas. 0.0 = the legacy path.
    rotate: float = Field(default=0.0, ge=-15.0, le=15.0)
    # Word indices (into the POST-CASE, whitespace-split content) styled
    # differently from the rest of the slot, at word granularity within the
    # same fitted layout. Which styling: emphasis_style below.
    emphasis: list[int] = Field(default_factory=list)
    emphasis_style: Literal["accent_color", "italic", "swap_face",
                            "larger"] = "accent_color"
    # swap_face only: the family the emphasized words render in (validated
    # against fonts.FAMILIES). Must stay "" for every other style — a set
    # value the renderer would ignore is authoring error, not a no-op.
    emphasis_font: str = ""
    # Word indices (into the POST-CASE, whitespace-split content) that must
    # START a new line — a hard, designed break, as opposed to the automatic
    # search's opinion. [] (the default, and every slot that predates the
    # field) keeps the search exactly as it was.
    #
    # WHY THIS HAD TO EXIST: both fit paths choose breaks by a scoring rule,
    # and neither rule can express a designed one. The uniform fit ranks
    # fitting splits by LOWEST WIDTH VARIANCE, which structurally refuses to
    # put a short connective alone on a line ("AND" by itself is the highest
    # variance a four-word title can produce). justify_stack ranks by least
    # wasted VERTICAL space, which is a different opinion but still an
    # opinion. The four-line poster stack — long / long / short connective /
    # long, the shape half the dark-fantasy shelf is set in — is therefore
    # unreachable by search at any zone width or size range. It is not a
    # tuning problem; it is a "the designer knows and the scorer cannot"
    # problem, so the fix is to let the designer say it.
    line_breaks: list[int] = Field(default_factory=list)

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

    @model_validator(mode="after")
    def _line_breaks_wellformed(self) -> TextSlot:
        """Strictly increasing, every index at least 1 (a break BEFORE the
        first word is not a break), and — when content is present, the same
        condition the emphasis contract uses, since an archetype's slot has
        none at load time — in range and within max_lines. A break list that
        forces more lines than the slot allows is authoring error, not
        something to silently clamp: the whole point of the field is that
        the author gets exactly the stack they asked for."""
        if any(i < 1 for i in self.line_breaks):
            raise ValueError(
                f"line_breaks indices must be >= 1 (a break before the "
                f"first word is not a break), got {self.line_breaks}")
        if self.line_breaks != sorted(set(self.line_breaks)):
            raise ValueError(
                f"line_breaks must be strictly increasing with no repeats, "
                f"got {self.line_breaks}")
        if self.line_breaks and len(self.line_breaks) + 1 > self.max_lines:
            raise ValueError(
                f"line_breaks {self.line_breaks} forces "
                f"{len(self.line_breaks) + 1} lines but max_lines is "
                f"{self.max_lines}")
        if self.line_breaks and self.content.strip():
            n_words = len(self.content.split())
            bad = [i for i in self.line_breaks if i >= n_words]
            if bad:
                raise ValueError(
                    f"line_breaks {bad} out of range — content has "
                    f"{n_words} word(s), so the last legal break is "
                    f"{n_words - 1}")
        return self

    @model_validator(mode="after")
    def _emphasis_wellformed(self) -> TextSlot:
        """§15.12's emphasis contract, enforced at spec time — never a
        runtime surprise: indices must name real words of the (case-
        preserving) whitespace split; `italic` needs the family to actually
        ship an italic companion; `swap_face` needs a registered
        emphasis_font, and emphasis_font means nothing under any other
        style. Content is validated only when present — an archetype's slot
        is built with content="" and filled at build_spec time, and every
        CoverSpec re-validation after that sees the real title."""
        if any(i < 0 for i in self.emphasis):
            raise ValueError(
                f"emphasis indices must be non-negative, got {self.emphasis}")
        if self.emphasis and self.content.strip():
            # upper/title/as_is all preserve whitespace, so the word count
            # of the raw content equals the post-case split's.
            n_words = len(self.content.split())
            bad = [i for i in self.emphasis if i >= n_words]
            if bad:
                raise ValueError(
                    f"emphasis indices {bad} out of range — content has "
                    f"{n_words} word(s) (indices are into the post-case "
                    f"whitespace split)")
        if self.emphasis and self.emphasis_style == "italic":
            if not FAMILIES[self.font_family].italic_file:
                raise ValueError(
                    f"emphasis_style 'italic' but {self.font_family!r} "
                    f"ships no italic companion — pick a family with one, "
                    f"or use emphasis_style 'swap_face'")
        if self.emphasis_style == "swap_face":
            if self.emphasis and not self.emphasis_font:
                raise ValueError(
                    "emphasis_style 'swap_face' needs emphasis_font")
        elif self.emphasis_font:
            raise ValueError(
                f"emphasis_font ({self.emphasis_font!r}) is set but "
                f"emphasis_style is {self.emphasis_style!r} — the field "
                f"only applies to 'swap_face'")
        if self.emphasis_font and self.emphasis_font not in FAMILIES:
            raise ValueError(
                f"emphasis_font {self.emphasis_font!r} is not registered — "
                f"known families: {', '.join(sorted(FAMILIES))}")
        return self

    @model_validator(mode="after")
    def _fold_shadow_stroke(self) -> TextSlot:
        """§15.4's back-compat fold, ArtSlot._fold_mask_from's exact shape:
        when `effects` is non-empty, the legacy shadow folds onto the FRONT
        of the stack (painted first — deepest under-effect) and the legacy
        stroke onto the BACK (painted last — outermost over-effect), so
        compose has exactly one code path through the effects engine. Both
        legacy fields are left exactly as authored (archived specs and the
        autopilot, which keeps writing TextSlot.shadow, read them
        unchanged), and the fold is idempotent — a re-validation sees the
        folded entry already in place and changes nothing, which is what
        keeps a spec revalidating cleanly on every dump/validate round
        trip. An EMPTY stack folds nothing at all: that is the pre-wave
        spec shape, and compose's legacy path must keep reading the two
        fields directly, byte-identically."""
        if not self.effects:
            return self
        if self.shadow is not None:
            folded = effect_from_shadow(self.shadow)
            if self.effects[0] != folded:
                self.effects.insert(0, folded)
        if self.stroke is not None and self.stroke.width > 0:
            folded = effect_from_stroke(self.stroke)
            if self.effects[-1] != folded:
                self.effects.append(folded)
        return self


class GradientMask(BaseModel):
    """A synthesized soft mask (deep-stack wave, §15.2): `linear` ramps
    along `angle` (degrees, y-down image coordinates — the default 90 is
    "top-transparent → bottom-opaque", 0 ramps left→right); `radial` ramps
    with distance from `center` (canvas fractions), transparent core to
    opaque rim. `start`/`end` remap where along the ramp alpha begins
    rising and where it reaches 1.0, so a mask can hold a plate fully solid
    for most of its extent and fade only one edge — the two-plates-into-one-
    scene collage move this model exists for. Rendered by
    effects.gradient_mask at quarter scale + Lanczos (smooth by definition,
    the _GRAIN_SCALE discipline)."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["linear", "radial"] = "linear"
    angle: float = 90.0                # linear only
    # A pair as a list, never a tuple — same wire rule as ArtSlot.anchor
    # (OpenAI's strict structured-output mode rejects tuple-derived
    # prefixItems schemas), and the same [-2, 2] latitude: a radial mask
    # centered off-canvas is a legitimate design (a glow falling in from
    # beyond the trim).
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
    def _ramp_direction(self) -> GradientMask:
        if self.start >= self.end:
            raise ValueError(
                f"gradient mask start ({self.start}) must be strictly less "
                f"than end ({self.end}) — a zero-width or reversed ramp is "
                f"a constant, not a gradient (use invert for the reversed "
                f"reading)")
        return self


class MaskSpec(BaseModel):
    """A first-class mask (deep-stack wave, §15.2), attachable to any
    pixel-owning layer (ArtSlot.mask) or adjust layer (AdjustLayer.mask).
    Every set source resolves to one canvas-sized alpha field and they
    multiply together; `invert` applies last — so "the top third, but only
    inside the focal's silhouette" is two fields, no new grammar.

    - `from_layer`: an art slot id — that slot's POSITIONED alpha, hard-
      thresholded exactly like the legacy ArtSlot.mask_from stencil (which
      folds into this field at validation, byte-identically).
    - `gradient`: a synthesized soft ramp (GradientMask above).
    - `luminance_of`: an art slot id — its positioned luminance as alpha
      (bright areas keep, dark areas mask: light-driven region scoping).
    - `from_text` (§15.13 part 1): a TEXT slot id — the fitted glyph
      coverage, clipping an art layer INTO the letterforms (photo-in-the-
      title as a first-class art move; the mirror of TextSlot.mask_from).

    Resolution rules live on CoverSpec (existence, the from_layer ordering
    rule, the from_text↔mask_from cycle check) — a MaskSpec alone can only
    police its own shape, and at least one source must be set: an empty
    mask is always authoring error, never a deliberate no-op."""
    model_config = ConfigDict(extra="forbid")

    from_layer: str = ""
    gradient: GradientMask | None = None
    luminance_of: str = ""
    from_text: str = ""
    invert: bool = False
    # Gaussian blur applied to the RESOLVED mask, as a fraction of canvas
    # height; 0.0 (the default, and every pre-existing spec) leaves the mask
    # exactly as the sources produced it. `gradient` is soft by construction
    # and `luminance_of` is continuous, but `from_layer` is a hard 50%
    # threshold stencil and `from_text` is only glyph-antialiased — this is
    # the one knob that softens those two. Applied after the sources
    # multiply and before `invert`, so a feathered mask inverts to its own
    # feathered complement rather than to a hard edge.
    feather: float = Field(default=0.0, ge=0.0, le=0.25)

    @field_validator("from_layer", "luminance_of")
    @classmethod
    def _valid_slot_ref(cls, value: str) -> str:
        return _validate_slot_id(value) if value else value

    @model_validator(mode="after")
    def _some_source(self) -> MaskSpec:
        if not (self.from_layer or self.gradient is not None
                or self.luminance_of or self.from_text):
            raise ValueError(
                "mask sets no source — set at least one of from_layer, "
                "gradient, luminance_of, or from_text (or drop the mask)")
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
    # Flip the plate horizontally before any fit or placement. A generated
    # cutout's severed stem points whichever way the model happened to draw
    # it, and which way that is decides which trim edge the cut can be
    # carried out through — so this is placement machinery, not decoration.
    # (`corners` mirrors too, but into all four corners at once, which is a
    # different move entirely.)
    mirror: bool = False
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    # The full BLEND_MODES table (deep-stack wave, §15.1) — hue/color/
    # luminosity deferred, see that constant's comment.
    blend: Literal["normal", "multiply", "overlay", "soft_light", "screen",
                   "add", "lighten", "darken", "color_dodge"] = "normal"
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
                        "canvas", "speckle", "rule_frame", "frame_hairline",
                        "frame_thickthin", "frame_corners", "frame_deco",
                        "frame_octagon", "radial_glow", "light_leak",
                        "fog_gradient", "rays", "bokeh", "dust", "scratches",
                        "stars"] = ""

    # -- effects rack (§7.4a) — archetype/revision territory; a fresh
    # art-direction call only ever sets `treatment` (via ArtPrompt, folded in
    # by build_spec), never these four directly. ------------------------------
    treatment: Literal["none", "duotone", "silhouette", "posterize",
                       "sticker", "photo_soft"] = "none"
    mask_from: str = ""                        # another art slot's id, or "" = off
    # First-class mask (deep-stack wave, §15.2). `mask_from` above STAYS as
    # sugar for the common single-stencil case: when `mask` is unset,
    # _fold_mask_from below materializes it as mask.from_layer at
    # validation (byte-identical pixels — effects.resolve_mask keeps the
    # exact hard-threshold stencil semantics for from_layer), so compose
    # has exactly one mask code path. Setting both to different things is a
    # validation error; only the exact folded equivalent may coexist, which
    # is what keeps an already-validated spec revalidating cleanly on every
    # round-trip (revisions re-validate the whole document).
    mask: MaskSpec | None = None
    corners: bool = False                      # mirror into all four corners (transparent slots)
    # v2.2 wave, deliverable 1 (gravity-safe corners): by default, corners
    # placement keeps all four copies upright (only h-mirrored on the right
    # side) — a v-flipped bottom copy reads as gravity-defying for any
    # ornament whose own weight isn't top/bottom symmetric (a honey drip
    # pointing UP on the bottom corners, say). Set True to restore the
    # original full-mirror-into-all-four behavior, for a genuinely
    # symmetric ornament that wants it.
    corners_flip_vertical: bool = False
    scatter: int = Field(default=0)            # stamp N copies, 0 = off (transparent slots)
    # v2.2 wave, deliverable 3 (line-gap snap): "" = off (place normally via
    # anchor/scale/offset). "line_gap" only applies to a contain-fit slot
    # whose layer draws immediately after a text layer — it centers the
    # ornament in the largest real gap between that text's own fitted
    # lines instead of at a fixed anchor point that has no idea where the
    # glyphs actually landed. See compose._snap_to_line_gap.
    snap: Literal["", "line_gap"] = ""
    # v2.2 wave, deliverable 5 (texture shelf): names a
    # docproof.cover.textures.TEXTURES plate to draw when this slot has no
    # `asset` on disk — a third tier alongside `procedural` (checked first,
    # since a stocked plate is a more deliberate choice than a generic
    # procedural fallback), rendered per `texture_fit` and composited with
    # this slot's own opacity/blend like any other layer. "" = no opinion.
    texture_file: str = ""
    texture_fit: Literal["tile", "cover"] = "cover"
    # v2.2 wave, deliverable 7 (frame family + interactions): names another
    # art slot in this spec whose positioned alpha bbox (padded ~1.5%) gets
    # erased from THIS slot's own painted pixels — a frame politely
    # breaking around an emblem that overlaps it. "" = off. See
    # compose._apply_frame_notches.
    notch_for: str = ""
    # The ordered layer-style stack (§15.4), same model and same fixed
    # paint-order semantics as TextSlot.effects — a rim-lit cutout is
    # outer_glow HERE, on the focal slot itself (§15.5: one mechanism, not
    # a synth). Applied by compose after fit/placement/treatment/masks and
    # before compositing, so an effect always sees the slot's final shape.
    effects: list[Effect] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_slot_id(value)

    @field_validator("scatter")
    @classmethod
    def _scatter_range(cls, value: int) -> int:
        return _validate_scatter(value)

    @field_validator("texture_file")
    @classmethod
    def _known_texture(cls, value: str) -> str:
        """Mirrors TextSlot._known_family exactly (same shape: "" is always
        fine — no opinion — and a non-empty name must already be on the
        shelf) — "fails loudly at spec validation" for an unknown
        texture_file, per this wave's own acceptance test, rather than
        silently drawing nothing three steps later in compose()."""
        if value and value not in TEXTURES:
            raise ValueError(
                f"texture_file {value!r} is not on the shelf — known "
                f"textures: {', '.join(sorted(TEXTURES)) or 'none'}")
        return value

    @model_validator(mode="after")
    def _fold_mask_from(self) -> ArtSlot:
        """§15.2's back-compat fold: legacy `mask_from` becomes
        `mask.from_layer` at validation when `mask` is unset, so every
        downstream reader (compose, the CoverSpec mask validators) sees ONE
        field. `mask_from` itself is left exactly as authored — archived
        specs and existing callers keep reading it — and because the fold
        is idempotent (a re-validation sees mask == the folded equivalent
        and changes nothing), a spec survives any number of dump/validate
        round-trips. Both set to DIFFERENT things is refused: two masks
        with an undocumented winner is exactly the silent ambiguity
        extra="forbid" exists to kill."""
        if not self.mask_from:
            return self
        folded = MaskSpec(from_layer=self.mask_from)
        if self.mask is None:
            self.mask = folded
        elif self.mask != folded:
            raise ValueError(
                f"art slot {self.id!r} sets both mask_from="
                f"{self.mask_from!r} and a mask that is not its exact "
                f"fold — set mask.from_layer (mask wins the vocabulary) "
                f"and drop mask_from")
        return self


class ScrimSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "halo" (v2.2 wave, deliverable 2): a radial soft darkening centered on
    # the protected zone, blurred at a scale that never leaves a
    # discernible edge anywhere — pure atmosphere behind text, never a
    # panel with soft corners. See compose._paint_halo_scrim.
    kind: Literal["gradient_down", "gradient_up", "vignette", "panel", "halo"] = "panel"
    zone: Zone | None = None                   # None = derived from the protected TextSlot
    protects: Literal["title", "subtitle", "author", "series"] | None = None
    strength: float = Field(default=0.0, ge=0.0, le=1.0)
    color_role: PaletteRole = PaletteRole.scrim


class AdjustLayer(BaseModel):
    """A layer that owns no pixels (deep-stack wave, §15.3): when the layer
    walk reaches it, compose computes `op` over the CURRENT composite and
    blends the result back through `mask` × `opacity` — result =
    composite × (1 − m·opacity) + op(composite) × (m·opacity). These are
    the 6-10 adjustment layers of a real cover PSD — what unifies an
    assembled collage into one image — and `color_wash` is the one op that
    instead composites a solid fill AS a layer through the full §15.1
    blend table (dodge/burn painting, when masked).

    Per-op parameters are FLAT fields (the strict-schema wire rule: no
    dicts, no tuples — OpenAI's structured-output mode rejects both open
    keys and tuple prefixItems). Fields the chosen `op` never reads are
    validated but inert — deliberately forgiving, so a patch edit that
    changes `op` can never strand the spec in an invalid state."""
    model_config = ConfigDict(extra="forbid")

    # Shares the art-slot id namespace (a LayerRef kind="adjust" must be
    # unambiguous about what it names) — CoverSpec._adjust_ids_resolve
    # refuses a collision with any ArtSlot.id, or a duplicate.
    id: str
    op: Literal["grade", "gradient_map", "color_wash", "vignette", "bloom",
                "blur"]
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    # §15.1's full table; read by color_wash ONLY (every other op mixes by
    # the equation above, which has no blend in it).
    blend: Literal["normal", "multiply", "overlay", "soft_light", "screen",
                   "add", "lighten", "darken", "color_dodge"] = "normal"
    mask: MaskSpec | None = None
    # -- flat per-op params ---------------------------------------------------
    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)   # grade
    contrast: float = Field(default=0.0, ge=-1.0, le=1.0)     # grade
    saturation: float = Field(default=0.0, ge=-1.0, le=1.0)   # grade
    temperature: float = Field(default=0.0, ge=-1.0, le=1.0)  # grade: warm↔cool
                                       # (±effects.TEMPERATURE_MAX_SHIFT/255 at |1|)
    stops: list[str] = Field(default_factory=list)   # gradient_map: 2-3 role-or-hex
    color: str = ""                    # color_wash/vignette ink: role or hex;
                                       # "" = the scrim role
    strength: float = Field(default=0.5, ge=0.0, le=1.0)      # vignette/bloom
    # Gaussian radius as a fraction of canvas height (bloom/blur). The 0.25
    # cap is an implementer's-choice bound: a quarter-canvas-height blur
    # already erases all structure, so anything past it is a typo'd value,
    # not a bigger look.
    radius: float = Field(default=0.02, ge=0.0, le=0.25)
    threshold: float = Field(default=0.75, ge=0.0, le=1.0)    # bloom: relative
                                       # luminance above which pixels glow

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _validate_slot_id(value)

    @field_validator("stops")
    @classmethod
    def _valid_stops(cls, value: list[str]) -> list[str]:
        """Shape-checked regardless of `op` (the forgiving-but-validated
        rule): every entry must be a role or hex, and the count must be 0
        (unused) or 2-3 (a ramp) — a single stop is a constant pretending
        to be a gradient, never meaningful under any op."""
        if len(value) not in (0, 2, 3):
            raise ValueError(
                f"stops must have 2 or 3 entries (or be empty when unused), "
                f"got {len(value)}")
        return [_validate_role_or_hex(stop) for stop in value]

    @field_validator("color")
    @classmethod
    def _valid_color(cls, value: str) -> str:
        return _validate_role_or_hex(value) if value else value

    @model_validator(mode="after")
    def _op_requirements(self) -> AdjustLayer:
        """The one place an op's REQUIRED input is enforced: gradient_map
        without stops has no ramp to map through, so it fails at spec time
        rather than rendering something invented. Every other op's params
        have workable defaults, so this stays a single check."""
        if self.op == "gradient_map" and not self.stops:
            raise ValueError(
                f"adjust layer {self.id!r} is a gradient_map with no stops "
                f"— give it 2 or 3 (role names or #rrggbb hexes, dark to "
                f"light)")
        return self


class LayerRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "adjust" (deep-stack wave, §15.3) walks like any other layer; its ref
    # names a CoverSpec.adjust entry's id.
    kind: Literal["art", "scrim", "text", "adjust"]
    ref: str                                    # ArtSlot.id / scrim index / TextSlot.id / AdjustLayer.id


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
    # Adjust layers (deep-stack wave, §15.3) — defaulted empty so every
    # pre-wave spec (and every archived job.json without the key) validates
    # and renders byte-identically.
    adjust: list[AdjustLayer] = Field(default_factory=list)
    scrims: list[ScrimSpec]
    text: list[TextSlot]
    layers: list[LayerRef]                      # explicit z-order, bottom first
    # Balance & symmetry (§15.10): which vertical axis this composition
    # declares. "center" snaps near-miss ink centers onto the canvas
    # midline; "left"/"right" snap leading/trailing ink edges onto the
    # `axis_x` rail (defaulting to 0.08/0.92 when axis_x is None — see
    # balance.resolve_axis_x; "center" never reads axis_x at all). None —
    # the default, and what every archived spec without the key validates
    # to — means PRE-WAVE behavior: the snap pass never runs, so a spec
    # that never declared an axis renders the exact bytes it rendered
    # before this wave existed (§15.0 constraint 2; a "center" default
    # would silently move any element already within tolerance). The
    # balance MEASUREMENTS still run for None — they are report-only and
    # change no pixels — reading it as the center composition every
    # pre-wave archetype in fact is.
    axis: Literal["center", "left", "right"] | None = None
    # The left/right rail as a fraction of canvas width. Validated for
    # shape whenever set, read only when axis is "left"/"right" — inert
    # otherwise, deliberately forgiving (AdjustLayer's own flat-params
    # doctrine: a patch edit that changes `axis` can never strand the
    # spec in an invalid state).
    axis_x: float | None = Field(default=None, ge=0.0, le=1.0)
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
        adjust_ids = {a.id for a in self.adjust}
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
            if ref.kind == "adjust" and ref.ref not in adjust_ids:
                raise ValueError(
                    f"layers references adjust layer {ref.ref!r}, which is "
                    f"not in this spec's adjust list "
                    f"({', '.join(sorted(adjust_ids)) or 'empty'})")
            if ref.kind == "scrim":
                if not ref.ref.isdigit() or int(ref.ref) >= len(self.scrims):
                    raise ValueError(
                        f"layers references scrim {ref.ref!r}, but this "
                        f"spec has {len(self.scrims)} scrim(s)")
        return self

    @model_validator(mode="after")
    def _adjust_ids_resolve(self) -> CoverSpec:
        """Adjust layers share the art-slot id namespace (§15.3) so a
        LayerRef is never ambiguous about what it names and a patch edit
        addressing "fx_vign" finds exactly one thing. Duplicates within the
        adjust list are refused for the same reason — compose keys adjust
        layers by id, and a silent last-one-wins would be exactly the kind
        of three-steps-later surprise this module's validators exist to
        kill."""
        art_ids = {a.id for a in self.art}
        seen: set[str] = set()
        for layer in self.adjust:
            if layer.id in art_ids:
                raise ValueError(
                    f"adjust layer {layer.id!r} collides with an art slot "
                    f"of the same id — the two kinds share one namespace")
            if layer.id in seen:
                raise ValueError(
                    f"two adjust layers share the id {layer.id!r}")
            seen.add(layer.id)
        return self

    @model_validator(mode="after")
    def _masks_resolve(self) -> CoverSpec:
        """Every MaskSpec's references, resolved at the whole-spec level —
        the deep-stack successor to the launch-era _mask_from_precedes
        (whose ordering rule and error wording survive verbatim inside it:
        ArtSlot._fold_mask_from means a legacy `mask_from` arrives here AS
        `mask.from_layer`, so this one validator covers both spellings).
        Checked here, not on MaskSpec, because existence and "precedes"
        only mean anything relative to the whole art/text/layers picture,
        which MaskSpec can't see. Per §15.2:

        - `from_layer` must name a real art slot AND appear earlier in
          `layers` than the masked entity itself (its pixels must already
          be positioned when the mask is taken) — for art slots this is
          exactly §7.4a's rule; an adjust layer inherits it unchanged.
        - `luminance_of`/`from_text` need existence only: compose positions
          every art slot's pixels and resolves every text slot's ink
          before any mask is applied (_text_mask_from_resolves' reasoning),
          so draw order genuinely does not matter for them.
        - `from_text` on an ART slot additionally refuses the one true
          cycle (§15.13): the named text slot must not itself be
          mask_from-clipped to this same art slot — art-in-the-glyphs of a
          title that is itself clipped to that art is two mirrors facing
          each other, and whichever won would surprise somebody."""
        art_ids = {a.id for a in self.art}
        text_ids = {t.id for t in self.text}
        text_by_id = {t.id: t for t in self.text}
        first_position: dict[str, int] = {}
        for i, ref in enumerate(self.layers):
            if ref.kind in ("art", "adjust") and ref.ref not in first_position:
                first_position[ref.ref] = i

        def check(owner_kind: str, owner_id: str, mask: MaskSpec) -> None:
            if mask.from_layer:
                if mask.from_layer not in art_ids:
                    raise ValueError(
                        f"{owner_kind} {owner_id!r} has mask_from/"
                        f"mask.from_layer={mask.from_layer!r}, which is not "
                        f"an art slot in this spec (art slots: "
                        f"{', '.join(sorted(art_ids)) or 'empty'})")
                this_pos = first_position.get(owner_id)
                ref_pos = first_position.get(mask.from_layer)
                if this_pos is None or ref_pos is None or ref_pos >= this_pos:
                    raise ValueError(
                        f"{owner_kind} {owner_id!r}'s mask_from/"
                        f"mask.from_layer={mask.from_layer!r} must appear "
                        f"earlier in `layers` than {owner_id!r} itself, so "
                        f"its pixels are already positioned by the time "
                        f"{owner_id!r} is drawn")
            if mask.luminance_of and mask.luminance_of not in art_ids:
                raise ValueError(
                    f"{owner_kind} {owner_id!r} has mask.luminance_of="
                    f"{mask.luminance_of!r}, which is not an art slot in "
                    f"this spec (art slots: "
                    f"{', '.join(sorted(art_ids)) or 'empty'})")
            if mask.from_text and mask.from_text not in text_ids:
                raise ValueError(
                    f"{owner_kind} {owner_id!r} has mask.from_text="
                    f"{mask.from_text!r}, which is not a text slot in this "
                    f"spec (text slots: "
                    f"{', '.join(sorted(text_ids)) or 'empty'})")

        for slot in self.art:
            if slot.mask is None:
                continue
            check("art slot", slot.id, slot.mask)
            if slot.mask.from_text:
                container = text_by_id[slot.mask.from_text]
                if container.mask_from == slot.id:
                    raise ValueError(
                        f"art slot {slot.id!r} is clipped to text slot "
                        f"{slot.mask.from_text!r}'s glyphs "
                        f"(mask.from_text) while that text slot is itself "
                        f"mask_from-clipped to {slot.id!r} — pick one "
                        f"direction for the clip")
        for layer in self.adjust:
            if layer.mask is not None:
                check("adjust layer", layer.id, layer.mask)
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

    @model_validator(mode="after")
    def _notch_for_resolves(self) -> CoverSpec:
        """v2.2 wave, deliverable 7: ArtSlot.notch_for must name a real,
        DIFFERENT art slot in this spec. Unlike ArtSlot.mask_from
        (_mask_from_precedes, above), ordering never matters here —
        compose._apply_frame_notches runs as a finishing pass once every
        art slot is already positioned (a notch_for target frequently comes
        LATER in z-order than the frame itself: corner_vine/emblem draw
        after rule_frame in woven_emblem, for instance), so this checks
        existence and self-reference only, mirroring
        _text_mask_from_resolves' own "no precedes requirement" reasoning."""
        art_ids = {a.id for a in self.art}
        for slot in self.art:
            if not slot.notch_for:
                continue
            if slot.notch_for == slot.id:
                raise ValueError(
                    f"art slot {slot.id!r} cannot set notch_for to itself")
            if slot.notch_for not in art_ids:
                raise ValueError(
                    f"art slot {slot.id!r} has notch_for={slot.notch_for!r}, "
                    f"which is not an art slot in this spec (art slots: "
                    f"{', '.join(sorted(art_ids))})")
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
    # v2.1 BODY-fix wave: "<text id><-<art id>" -> fraction of that text
    # slot's own ink alpha covered by that art slot's alpha (the title
    # occlusion guard, fix 2) — only populated for a "sandwich" pair (a
    # contain-fit art slot immediately after a text layer); defaulted so
    # every pre-existing caller that builds a RenderReport by hand keeps
    # working unchanged.
    occlusion: dict[str, float] = Field(default_factory=dict)
    # The tallest contiguous vertical stretch of the finished cover with no
    # text/art/ornament ink crossing it, as a fraction of canvas height (the
    # dead-band metric, fix 4) — see docproof.cover.compose._dead_band_frac.
    dead_band_frac: float = Field(default=0.0, ge=0.0, le=1.0)
    # Every move the balance snap pass made (§15.10), one line per snap
    # with exact before→after numbers ("text 'title': ink center 51.20% →
    # 50.00% of width — snapped onto the center axis (-19px).") — the
    # warnings-adjacent info channel that keeps "why did it move" from
    # ever being a mystery. Kept SEPARATE from `warnings` because a snap
    # is a success, not a problem — but threaded alongside them into the
    # judge's composer_warnings channel (see pipeline._critique_and_revise)
    # so "near-miss alignment survived" is checkable against what actually
    # moved. Defaulted so every pre-existing caller that builds a
    # RenderReport by hand (and every archived job.json without the key)
    # keeps working unchanged.
    adjustments: list[str] = Field(default_factory=list)


# -- the art-direction call's answer -----------------------------------------

# The registry's family names, fixed at import time — built via create_model
# exactly the way docproof.prep.meta.detect_meta builds BookFacts.subject as
# a Literal over BookDesign.subject_choices: a family that does not exist on
# the shelf cannot be picked on the wire, schema-enforced rather than merely
# hoped for.
_FONT_FAMILY_NAMES: tuple[str, ...] = tuple(FAMILIES)

# The recipe shelf's names, fixed at import time, with "" (no recipe) always
# first — the same closed-Literal-via-create_model trick as the font roster
# above (§15.6): a finishing stack that does not exist cannot be picked on
# the wire, schema-enforced rather than merely hoped for.
_RECIPE_NAMES: tuple[str, ...] = ("", *sorted(RECIPES))


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
    # "photo_soft" (v2.2 wave) is the one treatment that makes a
    # photographic/photoreal art prompt allowed at all — see direction.py's
    # photorealism doctrine.
    treatment: Literal["none", "duotone", "silhouette", "posterize",
                       "sticker", "photo_soft"] = "none"
    # Direction-time mask intent (§15.13 part 2) — the closed, safe, tiny
    # slice of the §15.2 mask machinery the art-direction call may reach
    # (full MaskSpec freedom stays archetype/revision territory). "" = none.
    # build_spec maps: "blend_into_background" → a linear gradient mask on
    # this slot dissolving it into the background plate (the two-plate
    # collage move); "inside_title" → mask.from_text="title" (art living in
    # the letterforms); "inside_focal" → mask.from_layer on the archetype's
    # own `focal` slot (the double-exposure move) — existence- and
    # ordering-checked there, dropped with a log line when the archetype
    # can't honor it (the §6.1 surplus-prompt precedent: dropping costs
    # nothing, failing a whole multi-concept job over it is the wrong
    # trade). See _intent_mask below.
    mask_intent: Literal["", "blend_into_background", "inside_title",
                         "inside_focal"] = ""

    @field_validator("slot")
    @classmethod
    def _valid_slot(cls, value: str) -> str:
        return _validate_slot_id(value)


def _coerce_art_prompts(value: object) -> object:
    """Accept the {slot: prompt} dict shape everywhere except the wire."""
    if isinstance(value, dict):
        return [{"slot": k, "prompt": v} for k, v in value.items()]
    return value


_TOKEN_LAYOUT_NAMES: tuple[str, ...] = ("",) + tuple(sorted(TOKEN_LAYOUTS))

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
    # The finishing stack this concept wants (§15.6), by name from the
    # closed shelf — "" (the default) stays silent, letting the archetype's
    # own default `recipe:` apply; a non-"" pick always wins over it.
    # build_spec expands the choice into real fx_-prefixed spec layers.
    recipe=(Literal[*_RECIPE_NAMES], ""),
    # ONE signature typography move for the title (§15.12), mapped by
    # build_spec onto the title slot with safe parameters (arch → arc=0.18;
    # tilt → rotate=-6; emphasis → indices resolved from emphasis_word).
    # The Literal is single-valued by construction — the one-signature-move
    # rule is structural on the wire — and _title_type_move below guards
    # the one combination still expressible (a stray emphasis_word riding
    # alongside a different move), dropping the stray with a log line. Raw
    # TextSlot fields stay archetype/revision territory.
    type_move=(Literal["", "justify_stack", "arch", "tilt", "emphasis"], ""),
    # emphasis only: the ONE title word to style, matched case-insensitively
    # against the title's own words at build_spec time; a word the title
    # doesn't contain is dropped with a log line (the §6.1 surplus-prompt
    # precedent), never fatal.
    emphasis_word=(str, ""),
    # Where the title's lines break, as word indices that START a new line
    # (§15.12's missing half — see TextSlot.line_breaks for why no scorer
    # can infer this). [] leaves the automatic search alone. Folded onto the
    # title slot by build_spec, and dropped with a log line — never fatal,
    # the §6.1 surplus-prompt posture — when the indices do not fit the
    # title this book actually has.
    title_breaks=(list[int], []),
    # Where the two accent plates sit, from the closed TOKEN_LAYOUTS shelf
    # (§19.2) — the one placement decision a template of that shape hands
    # to the direction call, precisely so a catalogue of books built on one
    # template does not put its accents in the same two spots every time.
    # "" (the default) keeps whatever anchors the archetype itself declares,
    # so every existing archetype and every existing Direction is unchanged.
    # A pick naming slots the archetype doesn't declare is dropped with a log
    # line in build_spec, never fatal.
    token_layout=(Literal[*_TOKEN_LAYOUT_NAMES], ""),
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
    # Which purse this job's Anthropic model calls run on: "subscription"
    # (the owner's Claude login, $0 in API dollars) or "api" (metered
    # credits) — the RESOLVED lane, not the request, so the app can show a
    # person which one a run is spending and a revision reuses the lane its
    # job was started on rather than silently switching purses mid-book.
    # Empty on any job created before the lane was a choice, which reads as
    # "whatever the deployment does today".
    anthropic_lane: str = ""
    # Which rung of the image-quality ladder every generation this job makes
    # rolls at, and is priced at: "draft" (1K, ~3 cents — shop concepts
    # cheaply and sharpen the keeper later in Cover Canvas) or "full" (2K,
    # ~5 cents). Fixed when the job is created and never revisable, so one
    # job's ledger is quoted in one currency; see
    # docproof.cover.pipeline._image_tier, the single resolver every
    # generation and every image ledger row reads it through. Empty on any
    # job created before the tier was a choice, which reads as "full" —
    # exactly what those jobs actually rolled at.
    image_quality: str = ""
    status: Literal["directing", "working", "ready", "error"]
    error: str | None = None
    concepts: list[ConceptState] = Field(default_factory=list)
    ledger: list[dict[str, Any]] = Field(default_factory=list)   # {kind, detail, usd}
    created: str                                 # ISO UTC


# -- the merge: archetype + direction + brief -> spec ------------------------

# Reserved for recipe-expanded layers (§15.6) — hand-authored archetype
# slots may never carry it (archetypes.py refuses at load), purely so
# humans and the judge can see at a glance which layers are finishing, and
# so compose's §15.7 attenuation ladder (keyed on this exact prefix) only
# ever dims layers a recipe put there.
FX_PREFIX = "fx_"


# Every recipe field whose value IS the magnitude of the effect, and which
# `recipe_strength` therefore scales toward zero. Deliberately explicit
# rather than "every float": `radius`, `threshold` and `angle` are shapes,
# not amounts — halving a bloom's threshold makes it catch MORE of the
# image, and halving a gradient's angle points it somewhere else entirely.
_RECIPE_MAGNITUDE_FIELDS: tuple[str, ...] = (
    "opacity", "strength", "brightness", "contrast", "saturation",
    "temperature")


def _expand_recipe(name: str, strength: float = 1.0
                   ) -> tuple[list[ArtSlot], list[AdjustLayer], list[LayerRef]]:
    """One recipe's finish entries as real spec layers (§15.6: expansion,
    not indirection — the spec stays fully self-contained, the archival
    guarantee never depends on the recipe file existing later, and a §6.2
    patch edit reaches every expanded layer as an ordinary one-field edit).
    Each entry is instantiated through the real ArtSlot/AdjustLayer models
    — THIS is the deep validation recipes.py's shallow loader deliberately
    leaves to build_spec — and every id must wear the reserved FX_PREFIX.
    Returns (art slots, adjust layers, layer refs in stack order); all
    empty for "" or a name that has somehow left the shelf (the closed
    Literal makes the latter unreachable from the wire — defensive, the
    compose dangling-reference posture)."""
    recipe = RECIPES.get(name) if name else None
    if not recipe:
        return [], [], []
    art: list[ArtSlot] = []
    adjust: list[AdjustLayer] = []
    refs: list[LayerRef] = []
    for i, entry in enumerate(recipe["finish"]):
        kind, fields = next(iter(entry.items()))
        if strength != 1.0:
            fields = {k: (v * strength
                          if k in _RECIPE_MAGNITUDE_FIELDS
                          and isinstance(v, (int, float))
                          and not isinstance(v, bool)
                          else v)
                      for k, v in fields.items()}
        try:
            if kind == "art":
                slot = ArtSlot(**fields)
                art.append(slot)
                refs.append(LayerRef(kind="art", ref=slot.id))
                layer_id = slot.id
            else:   # "adjust" — the shallow loader admits no third kind
                layer = AdjustLayer(**fields)
                adjust.append(layer)
                refs.append(LayerRef(kind="adjust", ref=layer.id))
                layer_id = layer.id
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"recipe {name!r} finish[{i}] does not validate as "
                f"a{'n art slot' if kind == 'art' else 'n adjust layer'}: "
                f"{e}") from e
        if not layer_id.startswith(FX_PREFIX):
            raise ValueError(
                f"recipe {name!r} finish[{i}] id {layer_id!r} must carry "
                f"the reserved {FX_PREFIX!r} prefix")
    return art, adjust, refs


def _intent_mask(intent: str, slot: Any, archetype: Archetype) -> MaskSpec | None:
    """One ArtPrompt.mask_intent as a real MaskSpec for `slot` (an
    ArchetypeArt), or None — either "no intent" or "the archetype can't
    honor it," and every can't-honor path logs and drops rather than
    raising (§15.13's own instruction to follow the §6.1 surplus-prompt
    precedent: a whole multi-concept job must never die over one
    over-enthusiastic intent).

    - "blend_into_background" → a linear gradient mask, self-contained and
      unrefusable: the slot stays solid at its base and dissolves upward
      into the plate behind it (GradientMask's default 90° ramp — the
      two-plate collage move's conventional direction).
    - "inside_title" → mask.from_text="title": needs a title text slot
      (every shipped archetype has one — checked anyway) that is not
      itself mask_from-clipped to this same slot (CoverSpec's one true
      from_text cycle, refused there, dropped here).
    - "inside_focal" → mask.from_layer="focal": needs an art slot with the
      conventional `focal` id, distinct from this slot, appearing earlier
      in the archetype's `layers` than this slot (CoverSpec's from_layer
      ordering rule — a mask can only clip to pixels already positioned).

    A slot that already carries archetype-authored masking (`mask_from`) is
    never overridden — the template's own design wins (the caller handles
    the first-class `mask` field the same way before ever calling this)."""
    if not intent:
        return None
    if slot.mask_from:
        log.info("mask_intent %r on slot %r dropped: the %s archetype "
                 "already clips that slot (mask_from=%r).",
                 intent, slot.id, archetype.name, slot.mask_from)
        return None
    if intent == "blend_into_background":
        return MaskSpec(gradient=GradientMask())
    if intent == "inside_title":
        title = next((t for t in archetype.text if t.id == "title"), None)
        if title is None:
            log.info("mask_intent 'inside_title' on slot %r dropped: the %s "
                     "archetype declares no title text slot.",
                     slot.id, archetype.name)
            return None
        if title.mask_from == slot.id:
            log.info("mask_intent 'inside_title' on slot %r dropped: the %s "
                     "archetype's title is itself clipped to that slot — a "
                     "cycle.", slot.id, archetype.name)
            return None
        return MaskSpec(from_text="title")
    # "inside_focal" — the closed Literal admits no other value.
    art_ids = {a.id for a in archetype.art}
    first_position: dict[str, int] = {}
    for i, ref in enumerate(archetype.layers):
        if ref not in first_position:
            first_position[ref] = i
    if "focal" not in art_ids or slot.id == "focal":
        log.info("mask_intent 'inside_focal' on slot %r dropped: the %s "
                 "archetype has no distinct focal slot to clip into.",
                 slot.id, archetype.name)
        return None
    this_pos = first_position.get(slot.id)
    focal_pos = first_position.get("focal")
    if this_pos is None or focal_pos is None or focal_pos >= this_pos:
        log.info("mask_intent 'inside_focal' on slot %r dropped: the %s "
                 "archetype draws focal at or after that slot, and a mask "
                 "can only clip to pixels already positioned.",
                 slot.id, archetype.name)
        return None
    return MaskSpec(from_layer="focal")


# Punctuation shed when matching a direction's emphasis_word against the
# title's own words — the emphasis indices point at the whitespace split
# (punctuation and all, exactly what TextSlot renders), but "Lighthouse,"
# should still match a model that wrote "lighthouse".
_EMPHASIS_TRIM = ".,:;!?\"'()[]{}—–-…"


def _title_type_move(direction: Direction, title: str) -> dict[str, Any]:
    """Direction.type_move (§15.12) as TextSlot field overrides for the
    TITLE slot — the four safe-parameter mappings, plus the one-signature-
    move guard build_spec owns: the closed Literal already makes two moves
    inexpressible on the wire, and the one combination still expressible (a
    stray emphasis_word riding alongside a non-emphasis move) is dropped
    with a log line here rather than honored as a second move. Emphasis
    indices are resolved against the title's whitespace split (the same
    split TextSlot's own emphasis contract counts — case ops preserve
    whitespace, so the indices survive any `case`), matching case-
    insensitively with punctuation shed; a word the title doesn't contain
    drops the whole move with a log line (§15.12 says so verbatim), never
    fails the job.

    The overrides are feature-detected against TextSlot.model_fields
    before being handed back: the §15.12 typeset fields (fit_mode / arc /
    rotate / emphasis) land in a parallel PR of this same wave, and until
    they exist a requested move must degrade to a logged no-op — the
    surplus-prompt posture again — rather than crash every build_spec on a
    tree where only this module's half has landed."""
    move = getattr(direction, "type_move", "")
    word = (getattr(direction, "emphasis_word", "") or "").strip()
    if move != "emphasis" and word:
        log.info("Direction %r set emphasis_word=%r with type_move=%r; one "
                 "signature move per concept — the word is ignored.",
                 direction.concept_name, word, move or "")
    overrides: dict[str, Any] = {}
    if move == "justify_stack":
        overrides = {"fit_mode": "justify_stack"}
    elif move == "arch":
        overrides = {"arc": 0.18}
    elif move == "tilt":
        overrides = {"rotate": -6.0}
    elif move == "emphasis":
        if not word:
            log.info("Direction %r asked for the emphasis move but set no "
                     "emphasis_word; dropped.", direction.concept_name)
            return {}
        wanted = word.strip(_EMPHASIS_TRIM).casefold()
        indices = [i for i, w in enumerate(title.split())
                   if w.strip(_EMPHASIS_TRIM).casefold() == wanted]
        if not indices:
            log.info("Direction %r set emphasis_word=%r, which is not a "
                     "word of the title %r; dropped.",
                     direction.concept_name, word, title)
            return {}
        overrides = {"emphasis": indices, "emphasis_style": "accent_color"}
    if not overrides:
        return {}
    missing = sorted(set(overrides) - set(TextSlot.model_fields))
    if missing:
        log.info("type_move %r dropped: TextSlot does not carry %s yet "
                 "(the §15.12 typeset fields land in a parallel PR).",
                 move, ", ".join(missing))
        return {}
    return overrides


def _token_anchors(direction: Direction, archetype: Archetype
                   ) -> dict[str, tuple[float, float]]:
    """Direction.token_layout as per-slot anchor overrides, or {} — either
    "no pick" or "this archetype has no token pair", and the can't-honor
    path logs and drops rather than raising (the §6.1 surplus-prompt
    precedent: a multi-concept job must never die over one inapplicable
    placement choice).

    Only the two conventional TOKEN_SLOT_IDS are addressed, and only when
    the archetype declares BOTH: half a layout is worse than none — it would
    move one token off its diagonal and leave the other where the template
    put it, which is the one arrangement TOKEN_LAYOUTS exists to prevent."""
    name = getattr(direction, "token_layout", "")
    if not name:
        return {}
    declared = {slot.id for slot in archetype.art}
    if not set(TOKEN_SLOT_IDS) <= declared:
        log.info("token_layout %r dropped: the %s archetype does not declare "
                 "both of %s.", name, archetype.name, ", ".join(TOKEN_SLOT_IDS))
        return {}
    return dict(TOKEN_LAYOUTS[name])


def build_spec(direction: Direction, brief: Brief, archetype: Archetype) -> CoverSpec:
    """Merge one art-direction concept into its chosen archetype's template.

    The archetype supplies structure (zones, fitting rules, layer order); the
    direction supplies taste (palette, fonts, art prompts, whether to add
    texture); the brief supplies the words. `archetype` must be the Archetype
    named by `direction.archetype` — the caller (docproof.cover.pipeline's
    run_job) resolves that lookup once from archetypes.ARCHETYPES and passes
    the same object here rather than making build_spec re-fetch it.

    The direction call only ever picks two fonts, one hero face and one
    supporting face. By default `title` wears the hero and every other slot
    wears the supporting one; an archetype text slot may override that with
    `font_role`, which is how a template puts the author's name in the
    display face while the tagline stays in the serif."""
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
    # Direction-time mask intents (§15.13 part 2): one word per art prompt,
    # expanded into a real MaskSpec by _intent_mask — or dropped with a log
    # line when the archetype can't honor it. An archetype-authored mask
    # (the new first-class ArchetypeArt.mask, the §15.13 part 3 templates'
    # own vocabulary) always wins over an intent: the template's designed
    # clip is structure, and the intent is per-concept taste that only
    # applies where the template left the slot unmasked.
    intents = {p.slot: p.mask_intent for p in direction.art_prompts
               if getattr(p, "mask_intent", "")}

    token_anchors = _token_anchors(direction, archetype)

    art: list[ArtSlot] = []
    for slot in archetype.art:
        if slot.id == "texture" and not include_texture:
            continue
        if slot.mask is not None:
            if intents.get(slot.id):
                log.info("mask_intent %r on slot %r dropped: the %s "
                         "archetype authors that slot's mask itself.",
                         intents[slot.id], slot.id, archetype.name)
            mask = MaskSpec.model_validate(slot.mask.model_dump())
        else:
            mask = _intent_mask(intents.get(slot.id, ""), slot, archetype)
        art.append(ArtSlot(
            id=slot.id,
            prompt=(prompts.get(slot.id, "")
                    if slot.generatable else ""),
            transparent=slot.transparent,
            fit=slot.fit,
            mirror=slot.mirror,
            opacity=slot.opacity,
            blend=slot.blend,
            anchor=list(token_anchors.get(slot.id, slot.anchor)),
            scale=slot.scale,
            offset=slot.offset,
            treatment=prompt_treatments.get(slot.id, slot.treatment),
            mask_from=slot.mask_from,
            mask=mask,
            corners=slot.corners,
            corners_flip_vertical=slot.corners_flip_vertical,
            scatter=slot.scatter,
            snap=slot.snap,
            texture_file=slot.texture_file,
            texture_fit=slot.texture_fit,
            notch_for=slot.notch_for,
            procedural=slot.procedural,
            effects=[Effect(**e.model_dump()) for e in slot.effects]))

    # The template's own adjustment layers (the six-to-ten of a real cover
    # PSD). Recipe-expanded fx_ layers are appended after these below, so a
    # recipe always finishes ON TOP of whatever the template already did.
    adjust: list[AdjustLayer] = [
        AdjustLayer(**layer.model_dump()) for layer in archetype.adjust]

    scrims = [ScrimSpec(kind=s.kind, protects=s.protects, strength=s.strength)
             for s in archetype.scrims]

    # The direction's ONE signature type move (§15.12), as title-slot field
    # overrides — {} when no move was asked for, the move couldn't be
    # honored (a logged drop, see _title_type_move), or the §15.12 TextSlot
    # fields haven't landed yet.
    title_move = _title_type_move(direction, brief.title)

    # Designed line breaks for the title (§15.12). Validated against THIS
    # book's title here rather than left to TextSlot's own validator, so a
    # break list that fits the last book but not this one drops with a log
    # line instead of failing the whole job — the same surplus-prompt
    # posture _title_type_move takes for an emphasis_word the title lacks.
    title_breaks = list(getattr(direction, "title_breaks", []) or [])
    if title_breaks and "line_breaks" in TextSlot.model_fields:
        n_words = len(brief.title.split())
        ok = (title_breaks == sorted(set(title_breaks))
              and all(1 <= i < n_words for i in title_breaks))
        if ok:
            title_move = {**title_move, "line_breaks": title_breaks}
        else:
            log.info("Direction %r set title_breaks=%s, which is not a valid "
                     "set of break points for the %d-word title %r; dropped.",
                     direction.concept_name, title_breaks, n_words, brief.title)

    text: list[TextSlot] = []
    for slot in archetype.text:
        role = getattr(slot, "font_role", "") or (
            "title" if slot.id == "title" else "author")
        font = direction.title_font if role == "title" else direction.author_font
        # The archetype's own §15.12 fields first, then the direction's move
        # folded OVER them — `update`, not a second `**` — so a template that
        # arcs its title and a concept that asks for `tilt` resolve by
        # precedence (the concept wins) instead of raising TypeError on a
        # duplicate keyword. Read through getattr for the same reason
        # font_role is: a template that predates the fields still builds.
        fields: dict[str, Any] = dict(
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
            effects=[Effect(**e.model_dump()) for e in slot.effects],
            optional=slot.optional,
            mode=slot.mode,
            mask_from=slot.mask_from,
            fit_mode=getattr(slot, "fit_mode", "uniform"),
            arc=getattr(slot, "arc", 0.0),
            rotate=getattr(slot, "rotate", 0.0))
        if slot.id == "title":
            fields.update(title_move)
        text.append(TextSlot(**fields))

    art_ids = {a.id for a in archetype.art}
    adjust_ids = {a.id for a in archetype.adjust}
    layers: list[LayerRef] = []
    for ref in archetype.layers:
        if ref == "texture" and not include_texture:
            continue
        if ref.startswith("scrim:"):
            layers.append(LayerRef(kind="scrim", ref=ref.removeprefix("scrim:")))
        elif ref in art_ids:
            layers.append(LayerRef(kind="art", ref=ref))
        elif ref in adjust_ids:
            layers.append(LayerRef(kind="adjust", ref=ref))
        else:
            layers.append(LayerRef(kind="text", ref=ref))

    # The finishing recipe (§15.6): the direction's pick wins; "" — the
    # wire's "stayed silent" — falls back to the archetype's own default,
    # so a template can bake its shelf convention in (big_type wants
    # quiet_literary) while any concept may still choose differently. The
    # expansion lands ABOVE the whole stack, text included: real ArtSlot/
    # AdjustLayer entries appended to art/adjust and their LayerRefs at the
    # top of the z-order, so the spec archives self-contained and §6.2
    # patch edits reach every fx_ layer as an ordinary field.
    recipe_art, recipe_adjust, recipe_refs = _expand_recipe(
        direction.recipe or archetype.recipe,
        getattr(archetype, "recipe_strength", 1.0))
    art.extend(recipe_art)
    adjust.extend(recipe_adjust)
    layers.extend(recipe_refs)

    # The axis declaration (§15.10) rides from archetype to spec verbatim —
    # None stays None (pre-wave behavior, no snap pass) — so revisions can
    # change it per cover while an archetype that never declared one keeps
    # rendering byte-identical pixels. Direction never sets it: which axis
    # a TEMPLATE composes around is structure, not per-concept taste.
    return CoverSpec(
        archetype=archetype.name,
        concept_name=direction.concept_name,
        rationale=direction.rationale,
        palette=direction.palette,
        art=art, adjust=adjust, scrims=scrims, text=text, layers=layers,
        axis=archetype.axis, axis_x=archetype.axis_x)


__all__ = [
    "ART_SLOT_IDS", "ART_TREATMENTS", "BLEND_MODES", "FX_PREFIX",
    "TOKEN_LAYOUTS", "TOKEN_SLOT_IDS",
    "PROCEDURAL_KINDS",
    "Brief", "PaletteRole", "Palette", "Zone", "Shadow", "Stroke", "Effect",
    "GradientMask", "MaskSpec", "AdjustLayer",
    "TextSlot", "ArtSlot", "ScrimSpec", "LayerRef", "CoverSpec",
    "RenderReport", "Direction", "Directions", "ConceptState", "JobState",
    "build_spec", "effect_from_shadow", "effect_from_stroke",
]
