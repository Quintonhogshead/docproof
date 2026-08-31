"""Cover Canvas's document model: one JSON file per editing session.

A CanvasDoc is what the editor opens — a flat, bottom-to-top list of layers,
each one a thing a person can grab, drag, retype or delete. It is NOT a
CoverSpec: the pipeline's spec describes a cover as *structure plus taste*
(zones, fit rules, archetype conventions) and only a renderer can tell you
where anything actually landed, which is the wrong document to hand somebody
who wants to move the title 20% left. The canvas document instead states
where everything IS, in one coordinate convention, so the browser can draw it
and a drag can rewrite it (docs/cover_canvas_spec.md §2).

Coordinates are fractions, exactly like CoverSpec's — one convention
everywhere — with one deliberate difference this module owns: a Frame's x/y
are the CENTER of the layer box, not its top-left corner. Every editor
gesture (rotate, flip, scale from the middle) is expressed around a center,
and Zone's top-left origin would mean converting on both sides of every one
of them. `ingest` does that conversion once, at the boundary.

Two rules keep this document durable:

- `source_spec` is a plain dict, never re-validated against
  docproof.cover.model. A canvas doc archived today must still open in a year
  when CoverSpec has grown three more fields and forbidden two old ones —
  cover-model evolution must not brick the editor's own files. It is
  provenance, not a live model.
- Everything else is `extra="forbid"`, the cover model's own discipline: a
  stray key from the wire (the UI, the assistant, a hand edit) fails at
  validation with a sentence, not three steps later as a layer that silently
  ignored half of what it was told. `Effect` is the single exception, and
  says why on itself.

docproof.canvas.ops is the ONLY sanctioned way to mutate one of these; this
module just defines the shape, loads it, and saves it.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, TypeAdapter,
                      field_validator, model_validator)

from docproof.cover.fonts import FAMILIES
from docproof.utils.files import write_atomic

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# The current document format. Bumped only when an old canvas.json can no
# longer be read as-is; `load_doc` refuses a version from the future rather
# than silently dropping fields it has never heard of.
DOC_VERSION = 1


def _validate_hex(value: str) -> str:
    """Every color here is a literal #rrggbb string — the same rule
    docproof.cover.model._validate_hex enforces on a spec, restated rather
    than imported so this module owes the cover model nothing at import time
    (see `source_spec`: canvas documents must outlive cover-model churn).
    Alpha never rides in a color; it lives in whatever alpha/opacity field
    the model has."""
    if not _HEX_RE.match(value):
        raise ValueError(f"{value!r} is not a #rrggbb hex color")
    return value


def _validate_source(value: str) -> str:
    """A plate reference is resolved against the job directory and nowhere
    else. The cover pipeline writes its plates as `assets/c<n>_<slot>.png`
    (docproof.cover.pipeline._generate_art_slot), so this is a job-dir-
    RELATIVE posix path rather than a bare filename — but it is never
    absolute, never escapes upward, and never carries a drive letter or a
    backslash, because a canvas document is handed to a server that will
    open whatever it names."""
    if not value:
        raise ValueError("a plate source cannot be empty")
    if value.startswith("/") or value.startswith("\\") or ":" in value:
        raise ValueError(
            f"plate source {value!r} must be relative to the job directory, "
            f"never an absolute path")
    if "\\" in value:
        raise ValueError(
            f"plate source {value!r} must use forward slashes — it is a "
            f"posix path inside the job directory, not a filesystem path")
    if any(part in ("..", "") for part in value.split("/")):
        raise ValueError(
            f"plate source {value!r} must stay inside the job directory "
            f"(no empty or '..' path segments)")
    return value


def new_layer_id() -> str:
    """A fresh layer id: "ly_" + 6 hex characters. Short enough to read in a
    diff of the op log, wide enough (16.7M) that a hand-written layer and a
    generated one never collide inside one document — CanvasDoc refuses
    duplicates outright, so a collision would be a loud failure, not a silent
    one, but it should not happen at all."""
    return f"ly_{secrets.token_hex(3)}"


class Size(BaseModel):
    """The reference pixel size this document's fractions are read against —
    the plate resolution the cover job was generated at (compose's
    EBOOK_W/EBOOK_H for every job today). Nothing in the document is stored
    in pixels; this exists so the client knows the aspect ratio to draw and
    the export knows what "full resolution" meant."""
    model_config = ConfigDict(extra="forbid")

    w: int = Field(gt=0)
    h: int = Field(gt=0)


class Wrap(BaseModel):
    """The paperback wrap this document is laid out on, stated in inches.

    Inches because that is the unit KDP and IngramSpark templates speak: a
    person reads "6 × 9, 0.62in spine, 0.125in bleed" off the printer's own
    calculator and types those four numbers in. A wrap stated in pixels
    would have to be converted back before it could be checked against the
    printer, which is exactly the arithmetic nobody should be doing by hand
    on the thing that gets guillotined. `dpi` turns the inches into the
    pixel canvas the document's fractions are read against.

    THE PANEL GEOMETRY — the printed-wrap convention, the sheet as it lies
    flat with its outside face up: BACK panel left, SPINE center, FRONT
    panel right (fold the sheet around the block and the front lands face
    up, which is why the front is on the right and not the left):

        |<-bleed->|<- trim_w ->|<-spine->|<- trim_w ->|<-bleed->|
        |  bleed  |    BACK    |  SPINE  |   FRONT    |  bleed  |

    In inches from the sheet's left edge:

        back  x0 = bleed                    x1 = bleed + trim_w
        spine x0 = bleed + trim_w           x1 = bleed + trim_w + spine
        front x0 = bleed + trim_w + spine   x1 = x0 + trim_w = sheet_w - bleed

    and vertically every panel runs from `bleed` to `bleed + trim_h`. The
    sheet is `(2*trim_w + spine + 2*bleed) x (trim_h + 2*bleed)` inches,
    which at `dpi` is `sheet_size()` — and that IS `CanvasDoc.canvas`
    whenever a wrap is present. Nothing else in this model changes meaning
    when a document becomes a wrap: layers stay fractional boxes on one
    canvas, they are simply read against a bigger one. That is the whole
    payoff of the fractional geometry docs/cover_designer_spec.md §12
    insisted on from day one — the wrap needs no second coordinate
    convention.

    Every dimension is positive (a zero-width spine or a zero trim is not a
    thin book, it is a typo), and `dpi` is bounded to the range a printer
    would accept: 72 is screen resolution and already too coarse to print,
    600 is past what any of these houses ask for and past what a browser
    canvas will composite at wrap size."""
    model_config = ConfigDict(extra="forbid")

    trim_w_in: float = Field(gt=0.0)
    trim_h_in: float = Field(gt=0.0)
    spine_in: float = Field(gt=0.0)
    bleed_in: float = Field(default=0.125, gt=0.0)
    dpi: int = Field(default=300, ge=72, le=600)

    @property
    def sheet_in(self) -> tuple[float, float]:
        """The whole sheet in inches, (width, height) — back + spine + front
        + bleed on all four sides."""
        return (2 * self.trim_w_in + self.spine_in + 2 * self.bleed_in,
                self.trim_h_in + 2 * self.bleed_in)

    def sheet_size(self) -> Size:
        """The sheet as the pixel canvas, rounded to whole pixels. The one
        definition of what `CanvasDoc.canvas` must be on a wrapped document
        — every caller (the conversion, the set_wrap op, the exporter) asks
        here rather than restating the multiplication."""
        w_in, h_in = self.sheet_in
        return Size(w=round(w_in * self.dpi), h=round(h_in * self.dpi))

    def panel_edges_in(self) -> dict[str, tuple[float, float]]:
        """Each panel's (x0, x1) in inches from the sheet's left edge, in
        the printed order the docstring draws: back, spine, front."""
        back_x1 = self.bleed_in + self.trim_w_in
        spine_x1 = back_x1 + self.spine_in
        return {"back": (self.bleed_in, back_x1),
                "spine": (back_x1, spine_x1),
                "front": (spine_x1, spine_x1 + self.trim_w_in)}

    def trim_y_in(self) -> tuple[float, float]:
        """The trim's (top, bottom) in inches. Every panel shares it — the
        wrap is one sheet, cut once."""
        return (self.bleed_in, self.bleed_in + self.trim_h_in)


class Frame(BaseModel):
    """Where a layer's box sits, as fractions of the canvas.

    x/y are the box's CENTER (see the module docstring), w/h its size —
    w against canvas width, h against canvas height. `rotation` is degrees
    clockwise about that center.

    The center is allowed well outside 0-1: a plate deliberately hanging off
    the trim edge, or a cutout half out of frame, is a real design, and the
    [-2, 2] latitude is the same one ArtSlot.anchor/offset already take in
    the cover model. w/h are the ones that must be real: a zero-width box
    draws nothing, which is authoring error every time, never a deliberate
    way to hide a layer (that is `visible`)."""
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=-2.0, le=2.0)
    y: float = Field(ge=-2.0, le=2.0)
    w: float = Field(gt=0.0)
    h: float = Field(gt=0.0)
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    # The §4 corner pin: four canvas-fraction [x, y] points, TL, TR, BR, BL,
    # or None for an unpinned layer. This is a RENDER-side distortion and
    # nothing else — x/y/w/h/rotation stay authoritative for where the box
    # IS (they are what a drag, a nudge and every "20% left" still speak to),
    # and `corners` only refines how the pixels sit INSIDE that box, the way
    # a poster pinned to a wall in perspective still occupies its own
    # rectangle on the layer list. A client that cannot draw the pin draws
    # the box and is merely undistorted, never misplaced.
    corners: list[list[float]] | None = None

    @field_validator("corners")
    @classmethod
    def _four_corners(cls, value: list[list[float]] | None
                      ) -> list[list[float]] | None:
        """A pin is four [x, y] points or it is not a pin. Three points is a
        triangle and five is a mistake, and either one reaches the renderer
        as an index error in a browser nobody is watching — so the shape is
        checked here, with the corner order stated in the sentence because
        the order is the part a caller gets wrong. The same [-2, 2] latitude
        x/y take: a pinned corner may hang off the trim, like the box
        itself."""
        if value is None:
            return value
        if len(value) != 4:
            raise ValueError(
                f"corners must be exactly 4 points — top-left, top-right, "
                f"bottom-right, bottom-left — got {len(value)}")
        for i, point in enumerate(value):
            if len(point) != 2:
                raise ValueError(
                    f"corner {i} must be an [x, y] pair, got "
                    f"{len(point)} number(s)")
            for coordinate in point:
                if not -2.0 <= coordinate <= 2.0:
                    raise ValueError(
                        f"corner {i} coordinate {coordinate} is outside the "
                        f"-2 to 2 latitude a frame's own x/y take")
        return value


class Effect(BaseModel):
    """One client-rendered layer effect: a name and its parameters.

    The ONLY `extra="allow"` model in this package, deliberately. Effects are
    drawn in the browser (bevel, inner shadow, edge glow, contact shadow, and
    whatever the next doctrine button needs — docs/cover_canvas_spec.md §4),
    and that vocabulary will grow faster than a Python schema should churn.
    A document that fails to open because the client shipped an effect
    parameter this server has not heard of is a worse failure than an effect
    the server cannot describe; the server never renders these, so it does
    not need to understand them. Everything else in this module stays
    `extra="forbid"`."""
    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class Warp(BaseModel):
    """A text layer's baseline distortion, as a preset plus one dial — the
    §4 type warps. `amount` is signed so the same preset bows both ways
    (an arc that smiles or frowns) without a second name; 0.0 with any kind
    is flat, which is what makes the slider continuous through zero."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["none", "arc", "arch", "flag", "bulge"] = "none"
    amount: float = Field(default=0.0, ge=-1.0, le=1.0)


class Stop(BaseModel):
    """One alpha stop of a scrim gradient: `at` along the ramp, `alpha`
    there. Color never varies along a scrim — a scrim is one ink at varying
    opacity, which is what makes it a scrim and not a fill."""
    model_config = ConfigDict(extra="forbid")

    at: float = Field(ge=0.0, le=1.0)
    alpha: float = Field(ge=0.0, le=1.0)


class Gradient(BaseModel):
    """A linear alpha ramp across a scrim layer's own box. `angle` is in
    degrees, y-down: 0 ramps left-to-right, 90 ramps top-to-bottom — the
    same convention docproof.cover.model.GradientMask documents, so a
    designer reading either document means the same thing by 90.

    Two stops minimum, in non-decreasing `at` order: one stop is a constant
    pretending to be a gradient (the cover model's own _valid_stops rule),
    and unsorted stops have no reading a renderer could agree on."""
    model_config = ConfigDict(extra="forbid")

    angle: float = 90.0
    stops: list[Stop] = Field(min_length=2)

    @model_validator(mode="after")
    def _ordered(self) -> Gradient:
        positions = [s.at for s in self.stops]
        if positions != sorted(positions):
            raise ValueError(
                f"gradient stops must be in non-decreasing `at` order, got "
                f"{positions}")
        return self


class PlateVersion(BaseModel):
    """One superseded plate of an art layer, kept so a re-roll is never
    destructive (§5: "click to swap back"). Prompt travels with the pixels —
    a plate you cannot regenerate is a dead end."""
    model_config = ConfigDict(extra="forbid")

    source: str
    prompt: str = ""

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        return _validate_source(value)


class LayerBase(BaseModel):
    """What every layer has regardless of what it draws. `kind` lives on the
    subclasses (it is the union's discriminator, so each one pins its own
    single value) — everything here is the chrome the layer list and the
    transform handles operate on, identical for a plate and a paragraph."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = ""
    visible: bool = True
    # Locked means "the transform tools skip this" — the field a person sets
    # on the background plate so dragging the title never nudges it. Every op
    # but set_layer refuses a locked layer (see docproof.canvas.ops), which
    # is why unlocking is set_layer's job.
    locked: bool = False
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    frame: Frame
    effects: list[Effect] = Field(default_factory=list)


class ArtLayer(LayerBase):
    """A raster plate: the pipeline's generated art, or an inpainted or
    re-rolled successor. `prompt` is the ASSEMBLED prompt that made these
    pixels, carried on the layer rather than looked up, so every
    regeneration verb (re-roll, tweak-then-roll, region inpaint) always has
    the thing it needs to ask for more of the same."""
    kind: Literal["art"] = "art"

    source: str
    prompt: str = ""
    transparent: bool = False
    fit: Literal["cover", "contain", "stretch"] = "cover"
    plate_history: list[PlateVersion] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        return _validate_source(value)


class TextLayer(LayerBase):
    """Live text, rendered client-side from the same OFL faces the composer
    embeds (§2). It stays live text until export — that is the whole point of
    the canvas — so nothing here is a measurement: `size` is what the type
    is set at, not what a fit search decided.

    There is no auto-wrap in v1. "\\n" in `text` is the only line break, so
    what the client draws is exactly what the document says, and a resize
    never silently re-flows a title somebody hand-broke."""
    kind: Literal["text"] = "text"

    text: str = ""
    family: str
    style: Literal["regular", "bold", "italic"] = "regular"
    # Fraction of canvas HEIGHT — the unit TextSlot.size_min/size_max and
    # RenderReport.fitted_sizes already speak, so a number copied from a
    # render report means the same thing here.
    size: float = Field(gt=0.0)
    color: str
    tracking: float = 0.0        # em fraction (CoverSpec's em/1000 ÷ 1000)
    align: Literal["left", "center", "right"] = "center"
    line_height: float = Field(default=1.1, gt=0.0)
    warp: Warp = Field(default_factory=Warp)

    @field_validator("family")
    @classmethod
    def _known_family(cls, value: str) -> str:
        """The shelf is closed, exactly as it is for TextSlot.font_family:
        the client serves these faces as webfonts by name, so a family that
        is not on the shelf renders as whatever the browser falls back to —
        a silently wrong cover. Same error wording, deliberately."""
        if value not in FAMILIES:
            raise ValueError(
                f"family {value!r} is not registered — known families: "
                f"{', '.join(sorted(FAMILIES))}")
        return value

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return _validate_hex(value)


class ScrimLayer(LayerBase):
    """An alpha-gradient rectangle in one ink: the thing that goes behind
    type so the type reads (§5's scrim button, and every scrim the
    composer's legibility autopilot already added to the job being
    ingested). One color, varying alpha — see Gradient."""
    kind: Literal["scrim"] = "scrim"

    color: str
    gradient: Gradient

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return _validate_hex(value)


class FrameLayer(LayerBase):
    """An ornamental frame — rules, corner motifs, inset panels (§4's
    "bezels", first sense). A closed preset list, not a path language: the
    client draws these four with parameters, and the ornament library grows
    by adding a preset here and a draw function there, never by shipping
    vector data through the document.

    `stroke_w` is a fraction of canvas WIDTH (a rule reads by its thickness
    against the page's narrow dimension, and every one of these presets is
    drawn as an outline), `inset` how far in from the layer's own box the
    ornament sits."""
    kind: Literal["frame"] = "frame"

    preset: Literal["single_rule", "double_rule", "corner_serifs", "inset_panel"]
    stroke: str
    stroke_w: float = Field(gt=0.0)
    inset: float = Field(default=0.02, ge=0.0, lt=0.5)
    fill: str | None = None

    @field_validator("stroke", "fill")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        return _validate_hex(value) if value is not None else value


class ShapeLayer(LayerBase):
    """A plain vector rectangle or ellipse — panels, bars, dots, the
    geometry a designer reaches for that is not art and not type. `radius`
    is a corner radius as a fraction of the layer box's shorter side, and is
    inert on an ellipse (the forgiving-inert-fields rule the cover model's
    AdjustLayer sets: a field the chosen `shape` never reads is validated
    but ignored, so flipping rect↔ellipse can never strand the layer)."""
    kind: Literal["shape"] = "shape"

    shape: Literal["rect", "ellipse"]
    fill: str | None
    stroke: str | None = None
    stroke_w: float = Field(default=0.0, ge=0.0)
    radius: float = Field(default=0.0, ge=0.0, le=0.5)

    @field_validator("fill", "stroke")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        return _validate_hex(value) if value is not None else value

    @model_validator(mode="after")
    def _draws_something(self) -> ShapeLayer:
        """MaskSpec._some_source's rule, restated: a shape with neither fill
        nor stroke paints nothing, which is always authoring error and never
        a deliberate no-op — hiding a layer is `visible`."""
        if self.fill is None and self.stroke is None:
            raise ValueError(
                f"shape layer {self.id!r} has neither fill nor stroke, so it "
                f"draws nothing — give it one, or set visible=false")
        return self


# The discriminated union `layers` is a list of. Discriminating on `kind`
# (rather than letting pydantic try each member in turn) is what makes a
# malformed layer report "art layer is missing `source`" instead of five
# stacked union errors nobody can read.
Layer = Annotated[
    ArtLayer | TextLayer | ScrimLayer | FrameLayer | ShapeLayer,
    Field(discriminator="kind"),
]

_LAYER_ADAPTER: TypeAdapter[Any] = TypeAdapter(Layer)


def parse_layer(data: dict[str, Any]) -> Any:
    """One layer dict validated into its concrete model through the union.

    The single entry point for "a layer arrived from the wire": the UI's
    add-layer op, the assistant's, and ops.py's own re-validation after
    every mutation all go through here, so there is exactly one answer to
    what a valid layer is. Raises pydantic's ValidationError; ops.py is the
    layer that turns that into a sentence."""
    return _LAYER_ADAPTER.validate_python(data)


class CanvasDoc(BaseModel):
    """One editing session, persisted inside the cover job directory it was
    ingested from — `canvas.json` for the job's first concept, and
    `canvas_c<n>.json` for the others (app/routes/canvas.py:_session_path).
    One file per CONCEPT, not per job: a job's four concepts are four
    different covers, and a single session per job meant the second one you
    opened silently handed you the first one's document.

    `layers` is bottom-to-top, the order compose() walks a CoverSpec's own
    layer list, so "first in the list" means the same thing in both
    documents and the ingest conversion is a straight walk.

    `history` is append-only and made of plain dicts: every mutation that
    ever touched this document, in the exact op vocabulary the UI, the
    button shelf and the assistant all speak (§4's op log). Plain dicts and
    not a model on purpose — the op vocabulary grows, and an audit trail
    that refuses to load because it records an op this build no longer
    implements would be an audit trail that loses history."""
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=DOC_VERSION, ge=1)
    job_id: str = Field(min_length=1)
    # WHICH cover of that job. A cover job holds several concepts (§8 of the
    # designer spec) and each is a different cover, so each gets its own
    # editing session — a job with four concepts has up to four documents,
    # and this is the one thing that tells them apart. Defaults to 0 so
    # every document written before sessions were per-concept still loads,
    # and reads as the concept an unqualified open would have picked.
    concept: int = Field(default=0, ge=0)
    canvas: Size
    # None for a front-cover document (every document starts as one), a
    # Wrap once docproof.canvas.wrap.to_wrap has turned it into a full
    # paperback sheet. Its presence is the ONLY thing that changes about
    # how the rest of this document reads: `canvas` becomes the whole
    # sheet, and every layer's fractions are read against it. See Wrap.
    wrap: Wrap | None = None
    layers: list[Layer] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float = Field(default=0.0, ge=0.0)
    # The CoverSpec this was ingested from, frozen as a plain dict. Never
    # re-validated against docproof.cover.model — see the module docstring:
    # this is provenance, and cover-model evolution must not brick an old
    # editor document.
    source_spec: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_layer_ids(self) -> CanvasDoc:
        """Every op addresses a layer by id, so two layers sharing one is
        not a cosmetic problem — it is a document where "move ly_ab12" has
        two answers and whichever won would surprise somebody (the cover
        model refuses duplicate adjust-layer ids for the same reason)."""
        seen: set[str] = set()
        for layer in self.layers:
            if layer.id in seen:
                raise ValueError(f"two layers share the id {layer.id!r}")
            seen.add(layer.id)
        return self

    @model_validator(mode="after")
    def _canvas_is_the_sheet(self) -> CanvasDoc:
        """On a wrapped document the canvas IS the wrap sheet.

        Enforced rather than assumed, because everything downstream reads
        the two together: the client draws its panel guides from the wrap's
        inches and its layers from the canvas's pixels, and the PDF export
        divides one by the other to get a page size. A document where they
        disagree is a wrap that prints at the wrong physical size — a
        failure you would discover from the printer, which is the worst
        place to discover it. docproof.canvas.wrap.to_wrap and the
        `set_wrap` op both recompute the canvas from the wrap through
        Wrap.sheet_size(), so the only way to reach this error is by hand
        or from a client that changed one number and not the other."""
        if self.wrap is None:
            return self
        sheet = self.wrap.sheet_size()
        if (self.canvas.w, self.canvas.h) != (sheet.w, sheet.h):
            raise ValueError(
                f"this document's wrap is {self.wrap.trim_w_in}x"
                f"{self.wrap.trim_h_in}in with a {self.wrap.spine_in}in spine "
                f"and {self.wrap.bleed_in}in bleed, which is {sheet.w}x"
                f"{sheet.h}px at {self.wrap.dpi}dpi — but its canvas is "
                f"{self.canvas.w}x{self.canvas.h}px. On a wrap the canvas IS "
                f"the sheet; change the wrap through the set_wrap op, which "
                f"recomputes both together")
        return self

    def layer(self, layer_id: str) -> Any:
        """The layer with this id, or KeyError saying what is actually in
        the document — a lookup that fails should tell you what you could
        have asked for."""
        for candidate in self.layers:
            if candidate.id == layer_id:
                return candidate
        known = ", ".join(l.id for l in self.layers) or "none"
        raise KeyError(
            f"no layer {layer_id!r} in this canvas (layers, bottom to top: "
            f"{known})")


def load_doc(path: str | Path) -> CanvasDoc:
    """Read a canvas.json. A missing file raises FileNotFoundError naming it
    (Path's own message already does); malformed content raises pydantic's
    ValidationError, which names the offending field. A document written by
    a NEWER build is refused outright rather than loaded with its unknown
    fields dropped on the floor — extra="forbid" would already fail, but the
    version check fails with a sentence that says which way the mismatch
    runs."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    doc = CanvasDoc.model_validate_json(text)
    if doc.version > DOC_VERSION:
        raise ValueError(
            f"{path} is a version {doc.version} canvas document and this "
            f"build reads version {DOC_VERSION} — upgrade before opening it")
    return doc


def save_doc(doc: CanvasDoc, path: str | Path) -> None:
    """Write the document atomically — tmp file then os.replace, via the
    same docproof.utils.files.write_atomic the cover pipeline rewrites
    job.json with. The editor saves after every op batch while the client is
    free to reload at any moment; a reader must never see half a
    document."""
    write_atomic(Path(path), doc.model_dump_json(indent=2))


__all__ = [
    "DOC_VERSION", "CanvasDoc", "Size", "Wrap", "Frame", "Effect", "Warp", "Stop",
    "Gradient", "PlateVersion", "LayerBase", "ArtLayer", "TextLayer",
    "ScrimLayer", "FrameLayer", "ShapeLayer", "Layer", "parse_layer",
    "new_layer_id", "load_doc", "save_doc",
]
