"""Front cover -> full paperback wrap: the same document, on a bigger canvas.

The wrap was a design constraint from day one (docs/cover_designer_spec.md
§12): keep every coordinate fractional and the print wrap becomes "the same
spec applied per-panel on a bigger canvas" rather than a second geometry.
This module is that promise being collected. A front-only CanvasDoc goes in;
the same layers come out, remapped into the FRONT panel of a sheet that is
back + spine + front + bleed, with the panels the front cover never had
seeded around them.

Three decisions this module owns:

- **The conversion is one-way, and it is a conversion, not an op.** There is
  no `to_front`: a wrap that could be un-wrapped would have to decide what
  happens to the back copy and the spine type, and the honest answer is that
  it does not happen — you convert when the page count is known and the book
  is going to print. A doc that is already a wrap is refused rather than
  re-converted (WrapError), because re-converting would remap the front
  panel INTO the new front panel, i.e. shrink the cover into a corner of
  itself. What *is* adjustable afterwards is the spine, and that is the
  `set_wrap` op (docproof.canvas.ops) — a firmed-up page count must never
  need a re-conversion.
- **Everything lands through ordinary model construction.** The seeded
  layers are built as models, not applied as ops: `add_layer` is a mutation
  vocabulary for a document that already exists, and this function is making
  a different document. What it DOES do is append one `to_wrap` history
  record, so the audit trail shows the moment the doc became a wrap. That
  record is deliberately not a replayable op — `history` is plain dicts for
  exactly this reason (docproof.canvas.model.CanvasDoc).
- **`panels()` is the one panel-geometry answer.** The route returns it and
  the client draws its guides from it; neither one re-derives the fold lines
  from the inches, because two derivations of one fold line is one fold line
  too many.
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat, UnidentifiedImageError
from pydantic import ValidationError

from docproof.cover.fonts import FAMILIES

# The same regex the model enforces on every color field, imported rather
# than restated so a color this module picks is by construction one a
# ShapeLayer or a TextLayer will accept.
from .model import (_HEX_RE, ArtLayer, CanvasDoc, Frame, ScrimLayer,
                    ShapeLayer, TextLayer, Wrap, new_layer_id, parse_layer)

log = logging.getLogger("docproof.canvas.wrap")


class WrapError(Exception):
    """A conversion that must not happen, said as a sentence.

    Same discipline as CanvasIngestError and OpError: this text goes into an
    HTTP body and into the AI box, so it has to be the whole story."""


# How far in from the trim a person should keep anything that must survive
# the guillotine. 0.25in is what KDP's own cover guide asks for and Ingram's
# is not looser; VERIFY AGAINST THE PRINTER'S CURRENT TEMPLATE before a real
# print run — the same caveat §12 attaches to the spine-width table.
SAFE_MARGIN_IN = 0.25

# The spine type's size, as a fraction of the SPINE'S WIDTH: a 0.62in spine
# gets a title about 0.28in tall (≈20pt) and an author about 0.19in (≈13pt),
# which is the proportion a printed spine actually uses. Stated against the
# spine rather than against the sheet because the spine is the constraint —
# type that overflows it lands on the front cover's fold.
SPINE_TITLE_FRACTION = 0.45
SPINE_AUTHOR_FRACTION = 0.30

# How much of the sheet's HEIGHT each spine layer's line runs along, before
# rotation. A spine layer's `w` is measured against sheet width like every
# other layer's, but it is drawn vertically once the 90-degree rotation is
# applied, so these are converted through the sheet's own aspect — see
# _spine_layer.
SPINE_TITLE_RUN = 0.55
SPINE_AUTHOR_RUN = 0.28

# Where the two spine layers sit down the sheet. The title takes the middle
# of the spine (the printed convention, and the part of a shelved book at
# eye level); the author sits low, above the publisher's mark.
SPINE_TITLE_Y = 0.5
SPINE_AUTHOR_Y = 0.82

# The back panel's placeholder: what it says, and how tall its type is in
# inches (≈11.5pt, a body size — the back is copy, not a poster).
BACK_COPY_TEXT = "Back cover copy…"
BACK_COPY_HEIGHT_IN = 0.16

# The body face the back-copy placeholder falls back to when the front
# cover has no text layer to copy one from. On the shelf, and the one face
# there that is a text face rather than a display face.
_FALLBACK_FAMILY = "Spectral" if "Spectral" in FAMILIES else sorted(FAMILIES)[0]

# The ground color when nothing in the document will say what the cover's
# field is (see _field_color): a mid gray, which reads as "nobody chose
# this" rather than quietly impersonating a design decision.
_NEUTRAL_FIELD = "#808080"


def to_wrap(doc: CanvasDoc, wrap: Wrap, *,
            job_dir: str | Path | None = None) -> CanvasDoc:
    """A front-cover document as a full paperback wrap.

    Returns a NEW document; `doc` is left exactly as it was, so a caller
    that refuses the result (or fails to save it) has not damaged the
    session. Raises WrapError when `doc` is already a wrap.

    What happens, in order:

    1. The canvas becomes the whole sheet (Wrap.sheet_size()).
    2. Every existing layer is remapped into the FRONT panel — the front
       cover is the cover that already exists, and the wrap is the sheet
       grown around it. See _remap_to_front for the exact arithmetic.
    3. The panels the front never had are seeded: a full-sheet ground in
       the cover's own field color at the very bottom of the stack, a spine
       title and spine author copied from the front's type, and a back-copy
       placeholder inside the back panel's safe margin.

    `job_dir` is optional and is only ever used to SAMPLE — it is where the
    art layers' plates live, so the ground can be filled with the field
    plate's real mean color when the document carries no palette. Without
    it the ground falls back through the same ladder minus that rung
    (_field_color)."""
    if doc.wrap is not None:
        raise WrapError(
            f"this canvas is already a wrap ({doc.wrap.trim_w_in}x"
            f"{doc.wrap.trim_h_in}in, {doc.wrap.spine_in}in spine), and "
            f"converting it again would remap the whole wrap into its own "
            f"front panel. To change the spine or the bleed, use the "
            f"set_wrap op; to lay the book out at a different trim size, "
            f"start from the front cover again")

    sheet = wrap.sheet_size()
    front = _remap_to_front(doc, wrap)
    ground = _ground_layer(doc, wrap, job_dir)
    back = _back_layers(doc, wrap, ground.fill or _NEUTRAL_FIELD)
    spine = _spine_layers(doc, wrap)
    minted = [ground, *back, *spine]

    wrapped = CanvasDoc(
        version=doc.version,
        job_id=doc.job_id,
        canvas=sheet,
        wrap=wrap,
        # The ground goes UNDER the cover, the new panels' type goes over
        # it: bottom to top, ground / front cover / back copy / spine type.
        layers=[ground, *front, *back, *spine],
        # Deep-copied, ops.apply's own rule: the new document's audit trail
        # and its provenance must not be the same objects the old one is
        # still holding, or an edit to either would rewrite both.
        history=copy.deepcopy(doc.history),
        cost_usd=doc.cost_usd,
        source_spec=copy.deepcopy(doc.source_spec))
    wrapped.history.append({
        "op": "to_wrap",
        "wrap": wrap.model_dump(mode="json"),
        "canvas": {"w": sheet.w, "h": sheet.h},
        # Which layers this conversion MINTED, so a later reader can tell
        # the seeded panels apart from the front cover's own layers without
        # guessing from their names.
        "seeded": [layer.id for layer in minted],
    })
    return wrapped


def panels(wrap: Wrap) -> dict[str, Any]:
    """The wrap's geometry as canvas fractions — the one answer the route
    and the client both read.

    Every panel's x-range and the shared y-range are absolute fractions of
    the sheet, ready to draw as a guide. `bleed` and `safe` are INSETS, not
    positions, because that is how each is used:

        the sheet's trim box  = (bleed.x, bleed.y) to (1-bleed.x, 1-bleed.y)
        a panel's safe box    = (panel.x0 + safe.x, panel.y0 + safe.y)
                                to (panel.x1 - safe.x, panel.y1 - safe.y)

    `sheet` carries both units — inches for the printer, pixels for the
    canvas — so nobody has to multiply by dpi to check one against the
    other."""
    w_in, h_in = wrap.sheet_in
    size = wrap.sheet_size()
    top, bottom = wrap.trim_y_in()
    edges = wrap.panel_edges_in()
    out: dict[str, Any] = {
        "sheet": {"w_in": w_in, "h_in": h_in, "w_px": size.w, "h_px": size.h,
                  "dpi": wrap.dpi},
        "bleed": {"x": wrap.bleed_in / w_in, "y": wrap.bleed_in / h_in,
                  "inches": wrap.bleed_in},
        "safe": {"x": SAFE_MARGIN_IN / w_in, "y": SAFE_MARGIN_IN / h_in,
                 "inches": SAFE_MARGIN_IN},
    }
    for name, (x0, x1) in edges.items():
        out[name] = {"x0": x0 / w_in, "x1": x1 / w_in,
                     "y0": top / h_in, "y1": bottom / h_in}
    return out


# -- the front panel ----------------------------------------------------------

def _remap_to_front(doc: CanvasDoc, wrap: Wrap) -> list[Any]:
    """Every existing layer, moved into the front panel.

    The front-only canvas is read as the front cover's TRIM — the page as
    it is cut, with no bleed of its own — so it maps onto the front panel's
    trim rectangle and the wrap's bleed is new margin around it. That is
    why the ground layer exists: without something under the sheet, the
    0.125in the guillotine eats would be transparent.

    The map is one affine per axis, applied identically to the frame's
    center, its size, and each pinned corner:

        x' = (front_x0 + x * trim_w) / sheet_w      w' = w * trim_w / sheet_w
        y' = (bleed    + y * trim_h) / sheet_h      h' = h * trim_h / sheet_h

    (all in inches, front_x0 = bleed + trim_w + spine). Rotation, flips,
    effects and every other field ride across untouched — the box moves and
    shrinks, the layer does not change what it is. Note the one honest
    wrinkle: the two axes scale by different factors unless the front-only
    canvas has exactly the trim's aspect ratio, so a rotated layer's box is
    stretched the way the whole cover is. There is nowhere else for the
    difference to go, and it is the same stretch the person will see on the
    ground plane of the design itself."""
    sheet_w, sheet_h = wrap.sheet_in
    front_x0 = wrap.panel_edges_in()["front"][0]

    def map_x(value: float) -> float:
        return (front_x0 + value * wrap.trim_w_in) / sheet_w

    def map_y(value: float) -> float:
        return (wrap.bleed_in + value * wrap.trim_h_in) / sheet_h

    out: list[Any] = []
    for layer in doc.layers:
        # Dumped and rebuilt through the model, ops.py's own discipline: a
        # remap that produced a frame the renderer cannot draw fails here,
        # with the layer named, rather than in a browser nobody is watching.
        data = layer.model_dump()
        frame = data["frame"]
        corners = frame.get("corners")
        frame.update({
            "x": map_x(frame["x"]),
            "y": map_y(frame["y"]),
            "w": frame["w"] * wrap.trim_w_in / sheet_w,
            "h": frame["h"] * wrap.trim_h_in / sheet_h,
            "corners": ([[map_x(px), map_y(py)] for px, py in corners]
                        if corners is not None else None),
        })
        try:
            out.append(parse_layer(data))
        except ValidationError as e:
            raise WrapError(
                f"layer {layer.id!r} ({layer.name or layer.kind}) could not be "
                f"remapped onto the front panel: {e.errors()[0]['msg']}") from e
    return out


# -- the ground ---------------------------------------------------------------

def _ground_layer(doc: CanvasDoc, wrap: Wrap,
                  job_dir: str | Path | None) -> ShapeLayer:
    """The full-sheet ground, bottom-most in the stack.

    A wrap has three panels and the front cover's art only covers one of
    them, so without this the back and the spine are transparent — which
    exports as white paper and prints as a book with a bare back. One flat
    rectangle in the cover's own field color is the honest minimum: it is
    obviously a starting point rather than a design, and it makes the bleed
    edge continuous so the sheet can go to a printer as-is."""
    return ShapeLayer(
        id=new_layer_id(), name="wrap sheet",
        frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
        shape="rect", fill=_field_color(doc, job_dir))


def _field_color(doc: CanvasDoc, job_dir: str | Path | None) -> str:
    """The cover's dominant field color, found honestly, in this order:

    1. **The spec's palette.** `source_spec` keeps the whole CoverSpec the
       job was rendered from, and `palette.background` is the color the art
       director actually chose. Nothing measured beats a stated intent.
    2. **The field plate's mean, measured with Pillow.** No palette (a
       hand-built document, or one whose provenance has been cleared) means
       the only evidence is pixels, so the bottom-most art layer's plate —
       the field, in every document ingest produces — is opened and its
       channel means are taken. Needs `job_dir`, because a plate reference
       is a job-dir-relative path and this module resolves nothing on its
       own.
    3. **The bottom-most scrim or shape's own ink**, which is what ingest
       leaves behind when the field was procedural and had no plate at all.
       Deliberately not any layer with a color on it: a TEXT layer's color
       is the ink that has to READ against the field, so filling the sheet
       with it would produce the one color the cover guarantees is wrong.
    4. A neutral gray, said out loud in the log.
    """
    palette = doc.source_spec.get("palette")
    if isinstance(palette, dict):
        stated = palette.get("background")
        if isinstance(stated, str) and _HEX_RE.match(stated):
            return stated

    if job_dir is not None:
        for layer in doc.layers:
            if isinstance(layer, ArtLayer):
                sampled = _plate_mean(Path(job_dir) / layer.source)
                if sampled is not None:
                    return sampled
                break

    for layer in doc.layers:
        if not isinstance(layer, (ScrimLayer, ShapeLayer)):
            continue
        ink = getattr(layer, "fill", None) or getattr(layer, "color", None)
        if isinstance(ink, str) and _HEX_RE.match(ink):
            return ink

    log.info("canvas wrap: nothing in this document says what its field "
             "color is (no palette, no readable plate, no vector ink); the "
             "wrap ground is a neutral %s.", _NEUTRAL_FIELD)
    return _NEUTRAL_FIELD


def _plate_mean(path: Path) -> str | None:
    """One plate's mean color as #rrggbb, or None if it cannot be read.

    None rather than an exception: a missing plate is a good reason to fall
    through to the next rung of _field_color's ladder, and a bad reason to
    refuse to build a wrap."""
    try:
        with Image.open(path) as plate:
            mean = ImageStat.Stat(plate.convert("RGB")).mean
    except (OSError, UnidentifiedImageError, ValueError) as e:
        log.info("canvas wrap: %s could not be sampled for the wrap ground "
                 "(%s); trying the next source.", path, e)
        return None
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in mean[:3])


# -- the back panel -----------------------------------------------------------

def _back_layers(doc: CanvasDoc, wrap: Wrap, ground: str) -> list[Any]:
    """The back panel's placeholder copy, inside the safe margin.

    A placeholder rather than nothing, and rather than a guess at the blurb:
    the back panel of a fresh wrap is the one panel with no content at all,
    and an empty rectangle gives a person nothing to grab. This is a real
    text layer at a real body size in the safe box's top-left corner — type
    over it and the back is written."""
    sheet_w, sheet_h = wrap.sheet_in
    x0, x1 = wrap.panel_edges_in()["back"]
    top, _bottom = wrap.trim_y_in()
    source = _smallest_text(doc)
    # The safe margin is a printer's number, not a proportion, so on a trim
    # narrow enough for two of them to swallow the panel it is the margin
    # that gives — a placeholder with no width would fail validation and
    # take the whole conversion down with it.
    inset = min(SAFE_MARGIN_IN, (x1 - x0) / 4)

    size = BACK_COPY_HEIGHT_IN / sheet_h
    line_height = source.line_height if source is not None else 1.1
    block_h = size * line_height
    return [TextLayer(
        id=new_layer_id(), name="back cover copy",
        frame=Frame(x=(x0 + x1) / 2 / sheet_w,
                    y=(top + inset) / sheet_h + block_h / 2,
                    w=(x1 - x0 - 2 * inset) / sheet_w,
                    h=block_h),
        text=BACK_COPY_TEXT,
        family=source.family if source is not None else _FALLBACK_FAMILY,
        size=size,
        color=source.color if source is not None else _readable_on(ground),
        line_height=line_height,
        align="left")]


def _readable_on(background: str) -> str:
    """Black or white, whichever the given ground can carry.

    Only ever the last resort for the back placeholder's ink (the front
    cover's own type color is the first choice, and it exists on every
    document that came through ingest). Rec. 601 luma, the same rough
    perceptual weighting the composer's own contrast checks use — this is a
    placeholder needing to be visible, not a measured contrast ratio."""
    r, g, b = (int(background[i:i + 2], 16) for i in (1, 3, 5))
    return "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#ffffff"


# -- the spine ----------------------------------------------------------------

def _spine_layers(doc: CanvasDoc, wrap: Wrap) -> list[Any]:
    """The spine's title and author, copied off the front cover's own type.

    Copied, not invented: the title on the spine is the title on the front,
    in the front's face and ink, and the largest and smallest text layers
    are what those two are on every document ingest produces (a title is
    set big; a byline is set small). A document with NO text layers gets no
    spine type at all — an empty spine is a person's cue to add one, while
    a spine reading "Title" would be a guess that has to be noticed before
    it can be fixed. A document with exactly ONE text layer gets a spine
    title and no author, for the same reason.

    Line breaks are collapsed on the way across: a front title broken over
    three lines is still one line on a spine."""
    texts = sorted((l for l in doc.layers if isinstance(l, TextLayer)),
                   key=lambda l: l.size, reverse=True)
    if not texts:
        log.info("canvas wrap: this document has no text layers, so the "
                 "spine is left empty rather than guessed at.")
        return []
    out = [_spine_layer(texts[0], wrap, "spine title", SPINE_TITLE_FRACTION,
                        SPINE_TITLE_RUN, SPINE_TITLE_Y)]
    if len(texts) > 1:
        out.append(_spine_layer(texts[-1], wrap, "spine author",
                                SPINE_AUTHOR_FRACTION, SPINE_AUTHOR_RUN,
                                SPINE_AUTHOR_Y))
    return out


def _spine_layer(source: TextLayer, wrap: Wrap, name: str, height: float,
                 run: float, y: float) -> TextLayer:
    """One rotated spine layer, centered across the spine.

    The geometry, because rotation makes it worth stating: a Frame is the
    box BEFORE `rotation` is applied, so this layer is authored as an
    ordinary horizontal line of type and then turned 90 degrees clockwise
    about its own center — which is how a spine is read on a shelved book,
    top to bottom. That means `w` (a fraction of sheet WIDTH, as always) is
    the run of type that ends up VERTICAL, so it is converted through the
    sheet's aspect ratio: a run of 0.55 of the sheet's height in inches
    becomes `0.55 * sheet_h / sheet_w` of its width. `size` and `h` are the
    type's height, which ends up lying ACROSS the spine, so both are stated
    as a fraction of the spine's own width — the constraint that matters,
    since type wider than the spine wraps onto the covers."""
    sheet_w, sheet_h = wrap.sheet_in
    x0, x1 = wrap.panel_edges_in()["spine"]
    size = height * wrap.spine_in / sheet_h
    return TextLayer(
        id=new_layer_id(), name=name,
        frame=Frame(x=(x0 + x1) / 2 / sheet_w, y=y,
                    w=run * sheet_h / sheet_w,
                    h=size * source.line_height,
                    rotation=90.0),
        text=" ".join(source.text.split()),
        family=source.family, style=source.style, size=size,
        color=source.color, tracking=source.tracking,
        line_height=source.line_height, align="center")


def _smallest_text(doc: CanvasDoc) -> TextLayer | None:
    """The document's smallest text layer — the byline, on a cover that came
    through ingest — or None. The back placeholder borrows its face and ink
    from here so the back panel opens in the book's own voice."""
    texts = [l for l in doc.layers if isinstance(l, TextLayer)]
    return min(texts, key=lambda l: l.size) if texts else None


__all__ = ["WrapError", "SAFE_MARGIN_IN", "to_wrap", "panels"]
