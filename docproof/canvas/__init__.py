"""Cover Canvas: the layered editor behind Spell & Check's cover product.

The one-shot pipeline (docproof.cover) generates a cover; this package is
the last 80% — the part where a person reaches into the image and moves
something 20% to the left. Three modules, in the order a session uses them:

- `ingest` turns a finished cover job into a CanvasDoc;
- `model` is that document — layers, frames, live text;
- `ops` is the only way it ever changes, one small JSON vocabulary shared by
  the UI, the button shelf and the AI box, which is what makes every edit
  undoable the same way;
- `wrap` turns the finished front cover into a full paperback wrap — back,
  spine, front and bleed on one sheet — which the fractional geometry makes
  a remap rather than a rebuild.

See docs/cover_canvas_spec.md for the whole design.
"""
from __future__ import annotations

from .ingest import CanvasIngestError, ingest
from .model import (DOC_VERSION, ArtLayer, CanvasDoc, Effect, Frame,
                    FrameLayer, Gradient, Layer, LayerBase, PlateVersion,
                    ScrimLayer, ShapeLayer, Size, Stop, TextLayer, Warp, Wrap,
                    load_doc, new_layer_id, parse_layer, save_doc)
from .ops import OP_NAMES, OpError, apply, apply_many
from .wrap import WrapError, panels, to_wrap

__all__ = [
    "DOC_VERSION", "CanvasDoc", "Size", "Wrap", "Frame", "Effect", "Warp",
    "Stop", "Gradient", "PlateVersion", "LayerBase", "ArtLayer", "TextLayer",
    "ScrimLayer", "FrameLayer", "ShapeLayer", "Layer", "parse_layer",
    "new_layer_id", "load_doc", "save_doc",
    "OpError", "OP_NAMES", "apply", "apply_many",
    "CanvasIngestError", "ingest",
    "WrapError", "to_wrap", "panels",
]
