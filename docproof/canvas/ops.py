"""Cover Canvas's mutation vocabulary: every change to a document is an op.

One vocabulary, three callers. A drag in the browser, a click on the button
shelf, and a tool call from the AI box all produce the same small JSON dicts
and all go through `apply` — which is what makes undo/redo one mechanism
instead of three, and what makes anything the assistant does exactly as
reversible as anything a hand does (docs/cover_canvas_spec.md §4, §6). An
edit path that bypassed this would be an edit nobody can undo.

The rules the whole vocabulary shares:

- **Unknown anything is an error.** An unknown op name, an unknown layer id,
  a field the op does not define, a text op aimed at a plate: every one of
  them raises OpError with a sentence, because these dicts arrive from a
  browser and from a language model, and the failure mode this module exists
  to prevent is an op that half-applied or silently did nothing.
- **Locked layers refuse everything but `set_layer`.** Locking is how you
  stop nudging the background while dragging the title, so it has to hold
  against every transform — and `set_layer` is the one op that must still
  get through, since that is where unlocking lives.
- **Every mutation re-validates through pydantic.** A mutated layer is
  rebuilt with `parse_layer` rather than assigned field-by-field, so a bad
  value from the wire fails here, loudly, instead of at render time in a
  browser nobody is watching.
- **`apply_many` is all-or-nothing.** Batches come from the assistant, where
  a plan's fourth tool call failing after three landed would leave a
  document nobody planned. It validates the whole batch against a deep copy
  and swaps only on success.

One op — `set_wrap` — addresses the DOCUMENT rather than a layer, because
the thing it changes (the print wrap's spine, bleed and dpi) is the
document. It is here rather than in a route of its own for the reason
everything else is here: a spine that widened when the page count firmed up
has to be as undoable as a nudge.
"""
from __future__ import annotations

import copy
from typing import Any, Callable

from pydantic import ValidationError

from .model import CanvasDoc, Wrap, parse_layer


class OpError(Exception):
    """A rejected op, carrying a sentence a person can read.

    Always a sentence, never a code: this exception's text goes straight
    into the AI box and into the browser's error toast, so "layer 'ly_a91f'
    is locked" has to be the whole story."""


# Every op's own field names, beyond "op" itself and (where it applies)
# "layer_id". The union is closed on purpose — a field this table does not
# list is a typo or a hallucinated parameter, and either way the caller
# needs to hear about it rather than watch the op quietly ignore half its
# input.
_FRAME_FIELDS = ("x", "y", "w", "h", "rotation", "flip_h", "flip_v",
                 "corners")
_TEXT_FIELDS = ("text", "family", "style", "size", "color", "tracking",
                "align", "line_height", "warp")
_LAYER_FIELDS = ("name", "visible", "locked", "opacity")
_ART_FIELDS = ("source", "fit")
# The three vector kinds' own parameters. Each gets a typed op for the same
# reason set_text has one: rebuilding a layer with remove+add to recolor a
# scrim throws away its id, drops it to the top of the stack, and records an
# op log that reads like a deletion — so the surgical verb is the one both
# the shelf and the assistant should reach for.
_SCRIM_FIELDS = ("color", "gradient")
_FRAME_STYLE_FIELDS = ("preset", "stroke", "stroke_w", "inset", "fill")
_SHAPE_FIELDS = ("shape", "fill", "stroke", "stroke_w", "radius")
# An adjust layer's own parameters (§15.3). `op` is in the list because
# changing which adjustment a layer IS must not cost the layer its id and its
# place in the stack — the model's fields are deliberately forgiving about
# params the new op does not read, exactly so this op can be one edit.
_ADJUST_FIELDS = ("op_kind", "blend", "brightness", "contrast", "saturation",
                  "temperature", "stops", "color", "strength", "radius",
                  "threshold")

# The print wrap's three adjustable numbers, and the two that are not.
# `trim_w_in`/`trim_h_in` are listed in the op's vocabulary ONLY so that
# naming one gets the real explanation — trim is the book's size, and
# changing it is a different book, not a wider spine — instead of the
# generic "set_wrap does not take that" a stray field earns.
_SET_WRAP_SETTABLE = ("spine_in", "bleed_in", "dpi")
_SET_WRAP_FIXED = ("trim_w_in", "trim_h_in")

_OP_FIELDS: dict[str, tuple[str, ...]] = {
    "set_frame": _FRAME_FIELDS,
    "nudge": ("dx", "dy"),
    "set_text": _TEXT_FIELDS,
    "set_layer": _LAYER_FIELDS,
    "set_art": _ART_FIELDS,
    "set_scrim": _SCRIM_FIELDS,
    "set_frame_style": _FRAME_STYLE_FIELDS,
    "set_shape": _SHAPE_FIELDS,
    "set_adjust": _ADJUST_FIELDS,
    "set_mask": ("mask",),
    "set_effects": ("effects",),
    "add_layer": ("layer", "index"),
    "remove_layer": (),
    "reorder_layer": ("index",),
    "set_wrap": _SET_WRAP_SETTABLE + _SET_WRAP_FIXED,
}

# The two ops that name no existing layer: add_layer brings its own, and
# set_wrap addresses the document. Everything else addresses a layer, and
# everything else is therefore subject to the lock check — note that
# set_wrap moves LOCKED layers too, deliberately: locking stops a drag from
# nudging the background, and re-measuring the sheet is not a drag. A locked
# background that stayed put while the sheet grew around it would be the one
# thing on the wrap in the wrong place.
_NO_TARGET = frozenset({"add_layer", "set_wrap"})

# The one op a locked layer still accepts, because it is where unlocking
# lives (see the module docstring).
_LOCK_EXEMPT = frozenset({"set_layer"})


def _apply_one(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """One op, applied and recorded, with NO document-level check.

    The half `apply` and `apply_many` share. Split out so the batch path can
    run its cross-layer check once at the end instead of after every op —
    see apply_many for why an intermediate state is allowed to be invalid.
    """
    if not isinstance(op, dict):
        raise OpError(
            f"an op must be a dict like {{'op': 'nudge', ...}}, got "
            f"{type(op).__name__}")
    name = op.get("op")
    if name not in _OP_FIELDS:
        raise OpError(
            f"unknown op {name!r} — the vocabulary is: "
            f"{', '.join(sorted(_OP_FIELDS))}")
    _check_fields(name, op)
    _HANDLERS[name](doc, op)
    doc.history.append(copy.deepcopy(op))


def apply(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """Validate one op, apply it to `doc` in place, and record it.

    The op dict is appended to `doc.history` only after the mutation
    succeeded, and as a deep copy — history is an audit trail, and a caller
    that reuses and re-edits its op dict must not be able to rewrite what
    the document says already happened.

    A single op is its own atomic boundary, so the cross-layer check runs
    here. It runs AFTER the history append for a reason: the op did happen,
    and a document that fails the check is one this function must not leave
    behind — so the check raises and the caller (whose doc is a draft, or
    who reloads from disk) never commits it, while an in-memory doc that
    somebody kept still carries an honest log of what was attempted."""
    _apply_one(doc, op)
    _check_document(doc, str(op.get("op")))


def apply_many(doc: CanvasDoc, ops: list[dict[str, Any]]) -> None:
    """Apply a whole batch, or none of it.

    Everything runs against a deep copy first; `doc` is only touched once
    the last op has landed cleanly. That is what makes an assistant turn
    atomic: a five-op plan whose fourth op names a layer the third one
    deleted leaves the document exactly as it was, with one error naming the
    op that failed and its position in the batch.

    Four fields are swapped back, because those are the ones an op can
    touch: `layers` and `history` for every op, plus `wrap` and `canvas`,
    which `set_wrap` re-measures together. `cost_usd` is the caller's (an AI
    verb's price is charged where it is spent, not by the op that records
    it) and so is `source_spec`, which is provenance and never a mutation."""
    if not isinstance(ops, list):
        raise OpError(
            f"apply_many takes a list of ops, got {type(ops).__name__}")
    draft = doc.model_copy(deep=True)
    for i, op in enumerate(ops):
        try:
            _apply_one(draft, op)
        except OpError as e:
            raise OpError(
                f"op {i + 1} of {len(ops)} was refused, so none of the batch "
                f"was applied: {e}") from e
    # Checked ONCE, at the batch boundary, not after every op. A batch is
    # atomic, so the batch is the only state anyone ever sees — and checking
    # each step would forbid legitimate two-op edits that pass THROUGH an
    # invalid arrangement, "clip the plate into the title, and move the title
    # under it" being the obvious one.
    _check_document(draft, f"this batch of {len(ops)}")
    doc.layers = draft.layers
    doc.history = draft.history
    doc.wrap = draft.wrap
    doc.canvas = draft.canvas


# -- shared plumbing ----------------------------------------------------------

def _check_document(doc: CanvasDoc, what: str) -> None:
    """Re-validate the whole document after a mutation.

    Every other check in this file is layer-local — `_replace` rebuilds the
    one layer it touched through `parse_layer`, which catches a bad color or
    a missing field but cannot see the layer LIST. Some rules only exist
    across layers: unique ids, and (the reason this function was written)
    a mask may only name a layer below the one wearing it. Four ops can
    break that rule without touching a mask at all — reorder_layer,
    remove_layer, add_layer and set_mask — so the check belongs here, at the
    end of a mutation, rather than being restated in each of them.

    The cost is one pydantic re-parse of a document that holds a few dozen
    layers, on an edit a human just made. The alternative is a document
    that saves and then refuses to load, which is the failure this whole
    file is arranged to prevent."""
    try:
        CanvasDoc.model_validate(doc.model_dump())
    except ValidationError as e:
        raise OpError(
            f"{what} would leave the document invalid: "
            f"{_first_error(e)}") from e


def _check_fields(name: str, op: dict[str, Any]) -> None:
    """Refuse a field this op does not define. Cheap, and it turns a
    plausible-looking hallucination ({"op": "nudge", "dz": 0.1}) into an
    error instead of a no-op nobody notices until the cover ships."""
    allowed = {"op", *_OP_FIELDS[name]}
    if name not in _NO_TARGET:
        allowed.add("layer_id")
    stray = sorted(set(op) - allowed)
    if stray:
        raise OpError(
            f"{name} does not take {', '.join(repr(s) for s in stray)} — its "
            f"fields are: {', '.join(sorted(allowed - {'op'})) or 'none'}")


def _target(doc: CanvasDoc, op: dict[str, Any], name: str) -> tuple[Any, int]:
    """The layer this op addresses and its index, with both refusals every
    targeted op shares: an id that names nothing, and a locked layer."""
    layer_id = op.get("layer_id")
    if not isinstance(layer_id, str) or not layer_id:
        raise OpError(f"{name} needs a `layer_id` naming the layer to change")
    try:
        layer = doc.layer(layer_id)
    except KeyError as e:
        raise OpError(str(e.args[0])) from e
    if layer.locked and name not in _LOCK_EXEMPT:
        raise OpError(
            f"layer {layer_id!r} is locked, so {name} was refused — unlock it "
            f"first with set_layer (locked=false)")
    index = next(i for i, l in enumerate(doc.layers) if l.id == layer_id)
    return layer, index


def _changes(op: dict[str, Any], fields: tuple[str, ...], name: str
             ) -> dict[str, Any]:
    """The subset of `fields` this op actually sets. An op that sets none of
    them is refused rather than treated as a no-op: a UI or an assistant
    that meant to change something and named nothing has a bug, and a
    silently recorded do-nothing op in the history would hide it."""
    changes = {f: op[f] for f in fields if f in op}
    if not changes:
        raise OpError(
            f"{name} names nothing to set — give it at least one of: "
            f"{', '.join(fields)}")
    return changes


def _kind(layer: Any, name: str, kind: str, noun: str | None = None) -> None:
    """Refuse an op aimed at the wrong kind of layer, saying which kind it
    actually hit. The one refusal every typed parameter op shares: `set_text`
    on a plate and `set_shape` on a title are both a caller reading the layer
    list wrong, and the fix is always "aim at the other one"."""
    if layer.kind != kind:
        raise OpError(
            f"{name} was aimed at layer {layer.id!r}, which is a "
            f"{layer.kind} layer, not {noun or kind}")


def _typed(doc: CanvasDoc, op: dict[str, Any], name: str, kind: str,
           fields: tuple[str, ...], noun: str | None = None) -> None:
    """One kind-checked parameter op, end to end.

    Every typed verb is the same four steps in the same order — find the
    layer (which refuses a stranger id and a locked layer), check its kind,
    take the fields it actually named, rebuild it through the model — so
    they share one body rather than five copies that could drift apart on
    which check comes first."""
    layer, index = _target(doc, op, name)
    _kind(layer, name, kind, noun)
    changes = _changes(op, fields, name)
    data = layer.model_dump()
    data.update(changes)
    _replace(doc, index, data, name, layer.id)


def _replace(doc: CanvasDoc, index: int, data: dict[str, Any], name: str,
             layer_id: str) -> None:
    """Rebuild the mutated layer through the model and put it back.

    Rebuilding (rather than assigning onto the live object) is what gives
    the "fails loudly, not at render time" guarantee AND keeps a failed op
    from leaving a half-mutated layer behind: the document is only touched
    on the last line, after validation passed."""
    try:
        rebuilt = parse_layer(data)
    except ValidationError as e:
        raise OpError(
            f"{name} would leave layer {layer_id!r} invalid: "
            f"{_first_error(e)}") from e
    doc.layers[index] = rebuilt


def _first_error(e: ValidationError) -> str:
    """One pydantic error as a sentence. The full ValidationError repr is a
    multi-line report with a documentation URL in it; the AI box and the
    browser toast both want the one line that says what was wrong."""
    errors = e.errors()
    if not errors:                                          # pragma: no cover
        return str(e)
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "the layer"
    return f"{where}: {first['msg']}"


def _number(value: Any, field: str, name: str) -> float:
    """A float from the wire. bool is excluded explicitly — it is an int in
    Python, and `{"dx": true}` nudging a layer by one whole canvas width is
    exactly the kind of quiet nonsense this module refuses."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpError(
            f"{name}'s {field} must be a number, got "
            f"{type(value).__name__}")
    return float(value)


def _index(value: Any, name: str, high: int) -> int:
    """A list position from the wire, bounds-checked against `high`
    inclusive. Never clamped: an assistant asking for index 12 of a 6-layer
    document has miscounted, and quietly stacking the layer on top instead
    would hide that."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpError(
            f"{name}'s index must be an integer, got {type(value).__name__}")
    if not 0 <= value <= high:
        raise OpError(
            f"{name}'s index {value} is out of range — this canvas allows "
            f"0 to {high}")
    return value


# -- the ops ------------------------------------------------------------------

def _op_set_frame(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """The box, absolutely — including `corners`, the corner pin, whose one
    special value is None: `{"corners": null}` UNPINS the layer, which is why
    the pin is cleared through the same op that sets it rather than through
    an unpin op that could only ever mean this."""
    layer, index = _target(doc, op, "set_frame")
    changes = _changes(op, _FRAME_FIELDS, "set_frame")
    data = layer.model_dump()
    data["frame"].update(changes)
    _replace(doc, index, data, "set_frame", layer.id)


def _op_nudge(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """Relative motion, the arrow-key op. Deliberately separate from
    set_frame's absolute assignment: "20% left" and "at 30%" are different
    intents, and a history that records which one a person meant is a
    history you can read."""
    layer, index = _target(doc, op, "nudge")
    if "dx" not in op and "dy" not in op:
        raise OpError("nudge names no motion — give it dx, dy, or both")
    dx = _number(op.get("dx", 0.0), "dx", "nudge")
    dy = _number(op.get("dy", 0.0), "dy", "nudge")
    data = layer.model_dump()
    data["frame"]["x"] += dx
    data["frame"]["y"] += dy
    _replace(doc, index, data, "nudge", layer.id)


def _op_set_text(doc: CanvasDoc, op: dict[str, Any]) -> None:
    _typed(doc, op, "set_text", "text", _TEXT_FIELDS)


def _op_set_layer(doc: CanvasDoc, op: dict[str, Any]) -> None:
    layer, index = _target(doc, op, "set_layer")
    changes = _changes(op, _LAYER_FIELDS, "set_layer")
    data = layer.model_dump()
    data.update(changes)
    _replace(doc, index, data, "set_layer", layer.id)


def _op_set_art(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """`fit`, and the plate-history swap.

    `source` is NOT a file setter — it may only name a plate this layer
    already has (the one it is showing, or one on its history strip), so the
    op that changes which pixels a layer draws can never point it at an
    arbitrary path from the wire. New pixels come from the regeneration lane,
    which is where the money and the provenance are.

    The strip is a shelf, not a stack: hopping along it must neither reorder
    nor consume it — click back to plate 1, forward to plate 2, back to
    plate 1, and the strip reads the same all three times. (A swap is
    therefore NOT a re-roll run backwards: a re-roll pushes the plate it
    replaces onto the shelf, because it is minting a new one; a swap is only
    ever choosing among plates that already exist.) The ONE write the swap
    makes to the shelf is preservation: regen only shelves the plate it
    REPLACES, so the newest plate exists nowhere but `source` — swapping
    away from it without shelving it first would strand the one plate the
    person paid for most recently."""
    layer, index = _target(doc, op, "set_art")
    _kind(layer, "set_art", "art")
    changes = _changes(op, _ART_FIELDS, "set_art")
    shelved = None
    if "source" in changes:
        changes["source"] = _plate(layer, changes["source"])
        on_shelf = {old.source for old in layer.plate_history}
        if changes["source"] != layer.source and layer.source not in on_shelf:
            shelved = {"source": layer.source, "prompt": layer.prompt}
    data = layer.model_dump()
    data.update(changes)
    if shelved is not None:
        data["plate_history"] = data["plate_history"] + [shelved]
    _replace(doc, index, data, "set_art", layer.id)


def _plate(layer: Any, value: Any) -> str:
    """One of this layer's own plates, or an error naming the whole shelf.

    Listing the allowed set is the point: a swap that was refused is almost
    always a caller reading the strip wrong, and the sentence it gets back
    is the strip."""
    shelf = list(dict.fromkeys(
        [layer.source] + [old.source for old in layer.plate_history]))
    if not isinstance(value, str) or value not in shelf:
        raise OpError(
            f"set_art cannot point layer {layer.id!r} at {value!r} — a source "
            f"swap picks a plate this layer already has: {', '.join(shelf)}. "
            f"To make a new plate, re-roll it.")
    return value


def _op_set_scrim(doc: CanvasDoc, op: dict[str, Any]) -> None:
    _typed(doc, op, "set_scrim", "scrim", _SCRIM_FIELDS)


def _op_set_frame_style(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """A frame ornament's own parameters — named `set_frame_style` and not
    `set_frame` because `set_frame` is the box every layer has, and one word
    cannot mean both "move this thing" and "make the rule thicker"."""
    _typed(doc, op, "set_frame_style", "frame", _FRAME_STYLE_FIELDS,
           noun="a frame ornament")


def _op_set_shape(doc: CanvasDoc, op: dict[str, Any]) -> None:
    _typed(doc, op, "set_shape", "shape", _SHAPE_FIELDS, noun="a shape")


def _op_set_adjust(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """An adjust layer's own parameters (§15.3).

    The one op in this file whose wire name for a field differs from the
    model's: the model calls the adjustment `op`, and `op` is already this
    vocabulary's word for "which verb is this" — an op dict cannot carry two
    meanings of the same key. So the wire says `op_kind` and it is
    translated here, once, rather than renaming the model field and making
    a canvas document disagree with the CoverSpec it was ingested from."""
    layer, index = _target(doc, op, "set_adjust")
    _kind(layer, "set_adjust", "adjust", "an adjust layer")
    changes = _changes(op, _ADJUST_FIELDS, "set_adjust")
    if "op_kind" in changes:
        changes["op"] = changes.pop("op_kind")
    data = layer.model_dump()
    data.update(changes)
    _replace(doc, index, data, "set_adjust", layer.id)


def _op_set_mask(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """What a layer shows through, on ANY layer kind (§15.2).

    Not a `_typed` verb, because a mask is the one parameter every kind
    has — clipping a plate into the title and fading a scrim's edge are the
    same op. `mask: null` clears it, which is why the field is required
    rather than optional: an absent `mask` key would be indistinguishable
    from asking for nothing, and silently doing nothing is how a windowed
    plate ships full-bleed."""
    layer, index = _target(doc, op, "set_mask")
    if "mask" not in op:
        raise OpError(
            "set_mask needs a `mask` object (or mask: null to remove one)")
    data = layer.model_dump()
    data["mask"] = op["mask"]
    _replace(doc, index, data, "set_mask", layer.id)


def _op_set_effects(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """Replaces the whole stack rather than appending to it: effect order is
    paint order, so "add a shadow" and "reorder the stack" are the same
    edit, and one op that states the finished stack is both simpler to undo
    and impossible to apply twice by accident."""
    layer, index = _target(doc, op, "set_effects")
    if "effects" not in op:
        raise OpError("set_effects needs an `effects` list (empty clears it)")
    data = layer.model_dump()
    data["effects"] = op["effects"]
    _replace(doc, index, data, "set_effects", layer.id)


def _op_add_layer(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """`index` absent or None puts the layer on top, which is what every
    "add a scrim / add a frame" gesture means. The incoming dict must carry
    its own `id` (docproof.canvas.model.new_layer_id makes one): minting it
    here would make the op non-replayable — the same op applied twice would
    address two different layers, and the op log is what undo and the
    assistant both read."""
    raw = op.get("layer")
    if not isinstance(raw, dict):
        raise OpError(
            "add_layer needs a `layer` object — a full layer dict including "
            "its id (see new_layer_id) and kind")
    try:
        layer = parse_layer(raw)
    except ValidationError as e:
        raise OpError(
            f"add_layer's layer does not validate: {_first_error(e)}") from e
    if any(existing.id == layer.id for existing in doc.layers):
        raise OpError(
            f"add_layer's layer id {layer.id!r} is already in this canvas — "
            f"ids address layers, so they cannot repeat")
    index = op.get("index")
    if index is None:
        doc.layers.append(layer)
    else:
        doc.layers.insert(_index(index, "add_layer", len(doc.layers)), layer)


def _op_remove_layer(doc: CanvasDoc, op: dict[str, Any]) -> None:
    _, index = _target(doc, op, "remove_layer")
    del doc.layers[index]


def _op_reorder_layer(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """Move, not swap: the layer comes out and goes back in at `index`,
    which is how a layer-list drag reads. `index` is the destination in the
    finished list, so moving the bottom layer to len-1 puts it on top."""
    _, index = _target(doc, op, "reorder_layer")
    if "index" not in op:
        raise OpError("reorder_layer needs an `index` to move the layer to")
    dest = _index(op["index"], "reorder_layer", len(doc.layers) - 1)
    doc.layers.insert(dest, doc.layers.pop(index))


def _op_set_wrap(doc: CanvasDoc, op: dict[str, Any]) -> None:
    """Re-measure an existing print wrap, keeping every layer where it is.

    The op a firmed-up page count produces: the spine was estimated when the
    wrap was made and is now known, and re-converting the document to fix
    one number would throw away everything done to the back and the spine
    since. So this changes the numbers and remaps the sheet underneath.

    THE REMAP RULE — nothing moves on the panel it sits on:

    - A layer is assigned to a panel by where its CENTER sits on the OLD
      sheet (the pragmatic reading of "wholly left of the spine" / "wholly
      right of it"; a straddler is judged by its middle, which is the same
      answer for everything that is actually on one panel).
    - It then keeps its position measured from that panel: the back panel's
      layers keep their distance from the back trim edge, the front panel's
      from the front trim edge, and the spine's from the spine's CENTER —
      which is what keeps spine type centered as the spine widens.
    - Sizes are preserved in INCHES, so a 2-inch-wide layer is still two
      inches wide on a sheet that got half an inch wider; its fraction
      changes because the sheet did.
    - Vertically the sheet only moves when the bleed does, so every layer
      keeps its distance from the trim's top edge.
    - The one exception: a layer covering the WHOLE sheet keeps its
      fractions instead of its inches, so the wrap's ground still covers the
      wrap. A background that stayed 13 inches wide on a 13.5-inch sheet
      would leave a bare strip down one edge, which is the opposite of what
      "nothing moves" should mean for the thing that is the sheet.

    A dpi change moves nothing at all — it is the same sheet at a different
    resolution, so only `canvas` changes."""
    if doc.wrap is None:
        raise OpError(
            "set_wrap adjusts an existing print wrap, and this canvas is "
            "still a front cover — convert it to a wrap first, which is "
            "where the trim size and the first spine estimate are decided")
    fixed = [f for f in _SET_WRAP_FIXED if f in op]
    if fixed:
        raise OpError(
            f"set_wrap cannot change {', '.join(fixed)} — the trim size is "
            f"the book's size, so changing it is a different book, not a "
            f"re-measured wrap. It takes: {', '.join(_SET_WRAP_SETTABLE)}")
    changes = _changes(op, _SET_WRAP_SETTABLE, "set_wrap")
    data = doc.wrap.model_dump()
    data.update(changes)
    try:
        new_wrap = Wrap(**data)
    except ValidationError as e:
        raise OpError(
            f"set_wrap would leave the wrap invalid: {_first_error(e)}") from e

    layers = _rewrapped_layers(doc, doc.wrap, new_wrap)
    # Assigned last and together: a refusal above must leave the document
    # exactly as it was, and a canvas that disagreed with its wrap for even
    # one statement would be a document CanvasDoc itself refuses to load.
    doc.wrap = new_wrap
    doc.canvas = new_wrap.sheet_size()
    doc.layers = layers


# How close to the sheet's own edges a layer has to reach before it counts
# as BEING the sheet rather than sitting on a panel (see _op_set_wrap's
# rule). A hair of tolerance, because a full-bleed layer's 1.0 may have been
# through a JSON round trip and a scale gesture on the way here.
_FULL_SHEET_EPS = 1e-6


def _rewrapped_layers(doc: CanvasDoc, old: Wrap, new: Wrap) -> list[Any]:
    """Every layer's frame, remapped from the old sheet onto the new one.

    Built into a fresh list and returned rather than assigned in place, so
    a layer that would not validate after the remap refuses the whole op
    instead of leaving half a document behind."""
    old_w, old_h = old.sheet_in
    new_w, new_h = new.sheet_in
    old_edges, new_edges = old.panel_edges_in(), new.panel_edges_in()
    spine_x0, spine_x1 = old_edges["spine"]
    dy = new.bleed_in - old.bleed_in

    def shift_x(center_in: float) -> float:
        """How far this layer's panel moved, in inches."""
        if center_in < spine_x0:
            return new_edges["back"][0] - old_edges["back"][0]
        if center_in > spine_x1:
            return new_edges["front"][0] - old_edges["front"][0]
        return (sum(new_edges["spine"]) - sum(old_edges["spine"])) / 2

    out: list[Any] = []
    for layer in doc.layers:
        data = layer.model_dump()
        frame = data["frame"]
        x, y, w, h = frame["x"], frame["y"], frame["w"], frame["h"]
        spans_w = (x - w / 2 <= _FULL_SHEET_EPS
                   and x + w / 2 >= 1 - _FULL_SHEET_EPS)
        spans_h = (y - h / 2 <= _FULL_SHEET_EPS
                   and y + h / 2 >= 1 - _FULL_SHEET_EPS)
        dx = shift_x(x * old_w)

        # Bound as defaults, not captured: these close over the loop, and a
        # late-binding closure would remap every layer by the last one's
        # panel shift.
        def map_x(value: float, shift: float = dx,
                  whole: bool = spans_w) -> float:
            return value if whole else (value * old_w + shift) / new_w

        def map_y(value: float, whole: bool = spans_h) -> float:
            return value if whole else (value * old_h + dy) / new_h

        corners = frame.get("corners")
        frame.update({
            "x": map_x(x), "y": map_y(y),
            "w": w if spans_w else w * old_w / new_w,
            "h": h if spans_h else h * old_h / new_h,
            "corners": ([[map_x(px), map_y(py)] for px, py in corners]
                        if corners is not None else None),
        })
        try:
            out.append(parse_layer(data))
        except ValidationError as e:
            raise OpError(
                f"set_wrap would leave layer {layer.id!r} invalid: "
                f"{_first_error(e)}") from e
    return out


_HANDLERS: dict[str, Callable[[CanvasDoc, dict[str, Any]], None]] = {
    "set_frame": _op_set_frame,
    "nudge": _op_nudge,
    "set_text": _op_set_text,
    "set_layer": _op_set_layer,
    "set_art": _op_set_art,
    "set_scrim": _op_set_scrim,
    "set_frame_style": _op_set_frame_style,
    "set_shape": _op_set_shape,
    "set_adjust": _op_set_adjust,
    "set_mask": _op_set_mask,
    "set_effects": _op_set_effects,
    "add_layer": _op_add_layer,
    "remove_layer": _op_remove_layer,
    "reorder_layer": _op_reorder_layer,
    "set_wrap": _op_set_wrap,
}

# The two tables must describe the same vocabulary: _OP_FIELDS is what the
# wire may say, _HANDLERS is what actually happens. A name in one and not
# the other is a half-built op, caught at import rather than on the first
# call that reaches it.
assert set(_OP_FIELDS) == set(_HANDLERS)

OP_NAMES: tuple[str, ...] = tuple(sorted(_OP_FIELDS))

__all__ = ["OpError", "OP_NAMES", "apply", "apply_many"]
