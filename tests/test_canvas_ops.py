"""docproof/canvas/ops.py: the one mutation vocabulary.

These ops arrive from a browser and from a language model, so the suite is
mostly about refusals: an op that half-applied, silently did nothing, or left
a layer the renderer cannot draw is the failure mode the module exists to
prevent. The other half is the two guarantees the editor is built on -- the
history is append-only, and a batch is all-or-nothing.
"""
from __future__ import annotations

import pytest

from docproof.canvas.model import (ArtLayer, CanvasDoc, Frame, FrameLayer,
                                   Gradient, PlateVersion, ScrimLayer,
                                   ShapeLayer, Size, Stop, TextLayer, Wrap,
                                   new_layer_id)
from docproof.canvas.ops import OP_NAMES, OpError, apply, apply_many
from docproof.canvas.wrap import WrapError, panels, to_wrap

ART_ID = "ly_aaa111"
TEXT_ID = "ly_bbb222"
SHAPE_ID = "ly_ccc333"
SCRIM_ID = "ly_ddd444"
FRAME_ID = "ly_eee555"

FIRST_PLATE = "assets/c0_background.png"
SECOND_PLATE = "assets/canvas_ly_aaa111_1.png"
CURRENT_PLATE = "assets/canvas_ly_aaa111_2.png"
PIN = [[0.08, 0.12], [0.94, 0.05], [0.97, 0.88], [0.11, 0.95]]


def _doc(**overrides) -> CanvasDoc:
    """Three layers, bottom to top: a plate, a title, a panel."""
    data = dict(
        job_id="20260831T120000Z-a1b2c3",
        canvas=Size(w=1600, h=2560),
        layers=[
            ArtLayer(id=ART_ID, name="background",
                     frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                     source="assets/c0_background.png",
                     prompt="A smoky brass foundry at dusk.", transparent=False),
            TextLayer(id=TEXT_ID, name="title",
                      frame=Frame(x=0.5, y=0.7, w=0.84, h=0.18),
                      text="THE LIGHTHOUSE\nAT GULL POINT",
                      family="Playfair Display", size=0.08, color="#f5f1e8"),
            ShapeLayer(id=SHAPE_ID, name="panel",
                       frame=Frame(x=0.5, y=0.9, w=0.6, h=0.08),
                       shape="rect", fill="#101820"),
        ])
    data.update(overrides)
    return CanvasDoc(**data)


def _rolled_doc() -> CanvasDoc:
    """The same document after two re-rolls, exactly as regen leaves it: the
    layer shows the newest plate and the two it replaced are on the history
    strip, oldest first."""
    doc = _doc()
    art = doc.layer(ART_ID)
    art.plate_history.extend([
        PlateVersion(source=FIRST_PLATE, prompt="A smoky brass foundry."),
        PlateVersion(source=SECOND_PLATE, prompt="The foundry, colder."),
    ])
    art.source = CURRENT_PLATE
    return doc


def _vector_doc() -> CanvasDoc:
    """A scrim, a frame ornament and a panel -- the three kinds the typed
    parameter ops address, bottom to top."""
    return _doc(layers=[
        ScrimLayer(id=SCRIM_ID, name="title scrim",
                   frame=Frame(x=0.5, y=0.8, w=1.0, h=0.4), color="#000000",
                   gradient=Gradient(angle=90.0,
                                     stops=[Stop(at=0.0, alpha=0.0),
                                            Stop(at=1.0, alpha=0.35)])),
        FrameLayer(id=FRAME_ID, name="rule",
                   frame=Frame(x=0.5, y=0.5, w=0.9, h=0.9),
                   preset="single_rule", stroke="#c9a227", stroke_w=0.003),
        ShapeLayer(id=SHAPE_ID, name="panel",
                   frame=Frame(x=0.5, y=0.9, w=0.6, h=0.08),
                   shape="rect", fill="#101820"),
    ])


def _new_shape(**overrides) -> dict:
    data = dict(id=new_layer_id(), kind="shape", name="new panel",
                frame={"x": 0.5, "y": 0.5, "w": 0.4, "h": 0.1},
                shape="rect", fill="#c9a227")
    data.update(overrides)
    return data


# -- the vocabulary is closed -------------------------------------------------

def test_the_two_op_tables_describe_the_same_vocabulary():
    assert set(OP_NAMES) == {
        "set_frame", "nudge", "set_text", "set_layer", "set_art", "set_scrim",
        "set_frame_style", "set_shape", "set_adjust", "set_mask",
        "set_effects", "add_layer", "remove_layer", "reorder_layer",
        "set_wrap"}


def test_an_unknown_op_name_lists_what_is_available():
    doc = _doc()
    with pytest.raises(OpError) as excinfo:
        apply(doc, {"op": "teleport", "layer_id": ART_ID})
    assert "teleport" in str(excinfo.value)
    assert "reorder_layer" in str(excinfo.value)


def test_an_op_that_is_not_a_dict_is_refused():
    with pytest.raises(OpError, match="must be a dict"):
        apply(_doc(), ["nudge", ART_ID])


def test_a_field_the_op_does_not_define_is_refused_not_ignored():
    # A plausible-looking hallucination must not read as a successful no-op.
    with pytest.raises(OpError, match="dz"):
        apply(_doc(), {"op": "nudge", "layer_id": ART_ID, "dx": 0.1, "dz": 0.1})


def test_an_unknown_layer_id_names_the_layers_that_do_exist():
    with pytest.raises(OpError) as excinfo:
        apply(_doc(), {"op": "nudge", "layer_id": "ly_gone00", "dx": 0.1})
    assert ART_ID in str(excinfo.value)


def test_a_targeted_op_without_a_layer_id_is_refused():
    with pytest.raises(OpError, match="layer_id"):
        apply(_doc(), {"op": "nudge", "dx": 0.1})


# -- set_frame / nudge --------------------------------------------------------

def test_set_frame_assigns_absolutely_and_leaves_the_rest_alone():
    doc = _doc()
    apply(doc, {"op": "set_frame", "layer_id": TEXT_ID, "x": 0.3,
                "rotation": -6.0})
    frame = doc.layer(TEXT_ID).frame
    assert (frame.x, frame.rotation) == (0.3, -6.0)
    assert (frame.y, frame.w, frame.h) == (0.7, 0.84, 0.18)


def test_nudge_is_relative():
    doc = _doc()
    apply(doc, {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2, "dy": 0.05})
    frame = doc.layer(TEXT_ID).frame
    assert frame.x == pytest.approx(0.3)
    assert frame.y == pytest.approx(0.75)


def test_nudge_on_one_axis_only_leaves_the_other_alone():
    doc = _doc()
    apply(doc, {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2})
    assert doc.layer(TEXT_ID).frame.y == 0.7


def test_nudge_with_no_motion_at_all_is_refused():
    with pytest.raises(OpError, match="no motion"):
        apply(_doc(), {"op": "nudge", "layer_id": TEXT_ID})


def test_a_boolean_is_not_a_number_even_though_python_thinks_so():
    with pytest.raises(OpError, match="must be a number"):
        apply(_doc(), {"op": "nudge", "layer_id": TEXT_ID, "dx": True})


def test_set_frame_naming_no_fields_is_refused():
    with pytest.raises(OpError, match="names nothing to set"):
        apply(_doc(), {"op": "set_frame", "layer_id": TEXT_ID})


def test_a_frame_value_from_the_wire_fails_loudly_not_at_render_time():
    doc = _doc()
    with pytest.raises(OpError, match="invalid"):
        apply(doc, {"op": "set_frame", "layer_id": TEXT_ID, "w": 0.0})
    assert doc.layer(TEXT_ID).frame.w == 0.84       # nothing half-applied
    assert doc.history == []


# -- set_text / set_art: kind mismatches --------------------------------------

def test_set_text_edits_a_text_layer():
    doc = _doc()
    apply(doc, {"op": "set_text", "layer_id": TEXT_ID, "text": "A NEW TITLE",
                "family": "Spectral", "tracking": 0.06,
                "warp": {"kind": "arc", "amount": 0.5}})
    layer = doc.layer(TEXT_ID)
    assert (layer.text, layer.family, layer.tracking) == ("A NEW TITLE",
                                                          "Spectral", 0.06)
    assert (layer.warp.kind, layer.warp.amount) == ("arc", 0.5)


def test_set_text_on_a_plate_says_what_kind_it_actually_is():
    with pytest.raises(OpError, match="which is a art layer, not text"):
        apply(_doc(), {"op": "set_text", "layer_id": ART_ID, "text": "no"})


def test_set_art_on_a_text_layer_is_refused():
    with pytest.raises(OpError, match="not art"):
        apply(_doc(), {"op": "set_art", "layer_id": TEXT_ID, "fit": "contain"})


def test_set_art_changes_the_fit():
    doc = _doc()
    apply(doc, {"op": "set_art", "layer_id": ART_ID, "fit": "contain"})
    assert doc.layer(ART_ID).fit == "contain"


# -- set_art: the plate-history swap ------------------------------------------

def test_a_plate_from_the_history_strip_can_be_swapped_back_in():
    doc = _rolled_doc()
    apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": FIRST_PLATE})
    assert doc.layer(ART_ID).source == FIRST_PLATE


def test_hopping_around_the_strip_neither_reorders_nor_consumes_it():
    # The strip is a shelf of everything ever rolled, so "click back" has to
    # survive being clicked twice. The FIRST hop away from the newest plate
    # shelves it (regen only shelves what it replaces, so the newest exists
    # nowhere but `source` until then); after that one preservation write,
    # hopping is pure choice and the shelf never changes again.
    doc = _rolled_doc()
    before = [v.model_dump() for v in doc.layer(ART_ID).plate_history]
    for source in (FIRST_PLATE, SECOND_PLATE, FIRST_PLATE):
        apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": source})
        assert doc.layer(ART_ID).source == source
    after = [v.model_dump() for v in doc.layer(ART_ID).plate_history]
    assert after[:-1] == before
    assert after[-1]["source"] == CURRENT_PLATE


def test_the_newest_plate_stays_reachable_after_swapping_away():
    # The reachability rule the shelf exists for: swap to an old plate, and
    # the plate you just paid for must still be on the strip to come back to.
    doc = _rolled_doc()
    apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": FIRST_PLATE})
    apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": CURRENT_PLATE})
    assert doc.layer(ART_ID).source == CURRENT_PLATE


def test_swapping_to_the_plate_already_showing_is_allowed():
    doc = _rolled_doc()
    apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": CURRENT_PLATE})
    assert doc.layer(ART_ID).source == CURRENT_PLATE
    # A no-op swap shelves nothing -- the plate is still showing, not
    # stranded.
    assert len(doc.layer(ART_ID).plate_history) == 2


def test_a_source_this_layer_has_never_had_is_refused_with_the_shelf_listed():
    doc = _rolled_doc()
    with pytest.raises(OpError) as excinfo:
        apply(doc, {"op": "set_art", "layer_id": ART_ID,
                    "source": "assets/somebody_elses_plate.png"})
    message = str(excinfo.value)
    assert FIRST_PLATE in message and SECOND_PLATE in message
    assert CURRENT_PLATE in message
    assert "re-roll" in message
    assert doc.layer(ART_ID).source == CURRENT_PLATE
    assert doc.history == []


def test_set_art_is_a_swap_not_a_file_setter():
    # A path that would pass the model's own source validation is still not
    # this layer's plate, so it never reaches the model at all.
    with pytest.raises(OpError, match="already has"):
        apply(_doc(), {"op": "set_art", "layer_id": ART_ID,
                       "source": "assets/c1_focal.png"})


def test_a_layer_with_no_history_can_only_be_pointed_at_its_own_plate():
    doc = _doc()
    apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": FIRST_PLATE})
    assert doc.layer(ART_ID).source == FIRST_PLATE


def test_a_source_that_is_not_even_a_string_is_refused():
    with pytest.raises(OpError, match="already has"):
        apply(_rolled_doc(), {"op": "set_art", "layer_id": ART_ID,
                              "source": ["assets/c0_background.png"]})


def test_the_swap_and_the_fit_can_travel_in_one_op():
    doc = _rolled_doc()
    apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": FIRST_PLATE,
                "fit": "contain"})
    layer = doc.layer(ART_ID)
    assert (layer.source, layer.fit) == (FIRST_PLATE, "contain")


def test_set_art_naming_no_fields_is_refused():
    with pytest.raises(OpError, match="names nothing to set"):
        apply(_doc(), {"op": "set_art", "layer_id": ART_ID})


def test_a_locked_plate_refuses_the_swap_too():
    doc = _rolled_doc()
    apply(doc, {"op": "set_layer", "layer_id": ART_ID, "locked": True})
    with pytest.raises(OpError, match="is locked"):
        apply(doc, {"op": "set_art", "layer_id": ART_ID, "source": FIRST_PLATE})


def test_an_unregistered_family_from_the_wire_is_refused():
    doc = _doc()
    with pytest.raises(OpError, match="not registered"):
        apply(doc, {"op": "set_text", "layer_id": TEXT_ID,
                    "family": "Comic Sans MS"})
    assert doc.layer(TEXT_ID).family == "Playfair Display"


# -- set_layer / locking ------------------------------------------------------

def test_set_layer_edits_the_chrome():
    doc = _doc()
    apply(doc, {"op": "set_layer", "layer_id": ART_ID, "name": "the field",
                "opacity": 0.4, "visible": False})
    layer = doc.layer(ART_ID)
    assert (layer.name, layer.opacity, layer.visible) == ("the field", 0.4, False)


@pytest.mark.parametrize("op", [
    {"op": "set_frame", "x": 0.1},
    {"op": "nudge", "dx": 0.1},
    {"op": "set_effects", "effects": []},
    {"op": "remove_layer"},
    {"op": "reorder_layer", "index": 0},
], ids=lambda o: o["op"])
def test_a_locked_layer_refuses_every_op_but_set_layer(op):
    doc = _doc()
    apply(doc, {"op": "set_layer", "layer_id": ART_ID, "locked": True})
    with pytest.raises(OpError, match="is locked"):
        apply(doc, {**op, "layer_id": ART_ID})


def test_set_layer_is_how_you_unlock_a_locked_layer():
    doc = _doc()
    apply(doc, {"op": "set_layer", "layer_id": ART_ID, "locked": True})
    apply(doc, {"op": "set_layer", "layer_id": ART_ID, "locked": False})
    apply(doc, {"op": "nudge", "layer_id": ART_ID, "dx": 0.05})
    assert doc.layer(ART_ID).frame.x == pytest.approx(0.55)


def test_set_text_is_refused_on_a_locked_text_layer_too():
    doc = _doc()
    apply(doc, {"op": "set_layer", "layer_id": TEXT_ID, "locked": True})
    with pytest.raises(OpError, match="is locked"):
        apply(doc, {"op": "set_text", "layer_id": TEXT_ID, "text": "nope"})


# -- the corner pin, through set_frame ----------------------------------------

def test_set_frame_pins_the_corners():
    doc = _doc()
    apply(doc, {"op": "set_frame", "layer_id": ART_ID, "corners": PIN})
    assert doc.layer(ART_ID).frame.corners == PIN


def test_the_pin_leaves_the_box_where_it_was():
    doc = _doc()
    apply(doc, {"op": "set_frame", "layer_id": TEXT_ID, "corners": PIN})
    frame = doc.layer(TEXT_ID).frame
    assert (frame.x, frame.y, frame.w, frame.h) == (0.5, 0.7, 0.84, 0.18)


def test_null_corners_unpin_the_layer():
    doc = _doc()
    apply(doc, {"op": "set_frame", "layer_id": ART_ID, "corners": PIN})
    apply(doc, {"op": "set_frame", "layer_id": ART_ID, "corners": None})
    assert doc.layer(ART_ID).frame.corners is None


def test_a_pin_and_a_move_can_travel_in_one_op():
    doc = _doc()
    apply(doc, {"op": "set_frame", "layer_id": ART_ID, "x": 0.4,
                "corners": PIN})
    frame = doc.layer(ART_ID).frame
    assert frame.x == 0.4 and frame.corners == PIN


@pytest.mark.parametrize("bad", [
    [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
    [[0.1, 0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
    [[9.0, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
    "top-left to bottom-right",
], ids=["three-points", "a-triple", "off-the-map", "prose"])
def test_a_malformed_pin_from_the_wire_is_refused_loudly(bad):
    doc = _doc()
    with pytest.raises(OpError, match="invalid"):
        apply(doc, {"op": "set_frame", "layer_id": ART_ID, "corners": bad})
    assert doc.layer(ART_ID).frame.corners is None
    assert doc.history == []


# -- the typed parameter ops --------------------------------------------------

def test_set_scrim_edits_the_ink_and_the_ramp():
    doc = _vector_doc()
    apply(doc, {"op": "set_scrim", "layer_id": SCRIM_ID, "color": "#0b0f14",
                "gradient": {"angle": 270.0,
                             "stops": [{"at": 0.0, "alpha": 0.0},
                                       {"at": 0.6, "alpha": 0.2},
                                       {"at": 1.0, "alpha": 0.8}]}})
    scrim = doc.layer(SCRIM_ID)
    assert scrim.color == "#0b0f14"
    assert scrim.gradient.angle == 270.0
    assert [s.alpha for s in scrim.gradient.stops] == [0.0, 0.2, 0.8]


def test_set_scrim_on_a_shape_says_what_kind_it_actually_is():
    with pytest.raises(OpError, match="which is a shape layer, not scrim"):
        apply(_vector_doc(), {"op": "set_scrim", "layer_id": SHAPE_ID,
                              "color": "#000000"})


def test_set_scrim_naming_no_fields_is_refused():
    with pytest.raises(OpError, match="names nothing to set"):
        apply(_vector_doc(), {"op": "set_scrim", "layer_id": SCRIM_ID})


def test_set_scrim_is_refused_on_a_locked_scrim():
    doc = _vector_doc()
    apply(doc, {"op": "set_layer", "layer_id": SCRIM_ID, "locked": True})
    with pytest.raises(OpError, match="is locked"):
        apply(doc, {"op": "set_scrim", "layer_id": SCRIM_ID,
                    "color": "#0b0f14"})


def test_an_unsorted_gradient_from_the_wire_fails_here_not_at_render_time():
    doc = _vector_doc()
    with pytest.raises(OpError, match="invalid"):
        apply(doc, {"op": "set_scrim", "layer_id": SCRIM_ID,
                    "gradient": {"angle": 90.0,
                                 "stops": [{"at": 0.9, "alpha": 0.1},
                                           {"at": 0.2, "alpha": 0.8}]}})
    assert doc.layer(SCRIM_ID).gradient.stops[0].at == 0.0
    assert doc.history == []


def test_set_frame_style_edits_the_ornament():
    doc = _vector_doc()
    apply(doc, {"op": "set_frame_style", "layer_id": FRAME_ID,
                "preset": "double_rule", "stroke_w": 0.005, "inset": 0.04,
                "fill": "#101820"})
    ornament = doc.layer(FRAME_ID)
    assert (ornament.preset, ornament.stroke_w) == ("double_rule", 0.005)
    assert (ornament.inset, ornament.fill) == (0.04, "#101820")
    assert ornament.stroke == "#c9a227"          # untouched


def test_set_frame_style_on_a_scrim_is_refused():
    with pytest.raises(OpError, match="not a frame ornament"):
        apply(_vector_doc(), {"op": "set_frame_style", "layer_id": SCRIM_ID,
                              "preset": "single_rule"})


def test_set_frame_style_naming_no_fields_is_refused():
    with pytest.raises(OpError, match="names nothing to set"):
        apply(_vector_doc(), {"op": "set_frame_style", "layer_id": FRAME_ID})


def test_set_frame_style_is_refused_on_a_locked_ornament():
    doc = _vector_doc()
    apply(doc, {"op": "set_layer", "layer_id": FRAME_ID, "locked": True})
    with pytest.raises(OpError, match="is locked"):
        apply(doc, {"op": "set_frame_style", "layer_id": FRAME_ID,
                    "inset": 0.05})


def test_an_ornament_preset_that_is_not_on_the_shelf_is_refused():
    doc = _vector_doc()
    with pytest.raises(OpError, match="invalid"):
        apply(doc, {"op": "set_frame_style", "layer_id": FRAME_ID,
                    "preset": "art_deco_swirl"})
    assert doc.layer(FRAME_ID).preset == "single_rule"


def test_set_shape_edits_the_geometry():
    doc = _vector_doc()
    apply(doc, {"op": "set_shape", "layer_id": SHAPE_ID, "shape": "ellipse",
                "fill": "#c9a227", "stroke": "#101820", "stroke_w": 0.002,
                "radius": 0.1})
    panel = doc.layer(SHAPE_ID)
    assert (panel.shape, panel.fill, panel.stroke) == ("ellipse", "#c9a227",
                                                       "#101820")
    assert (panel.stroke_w, panel.radius) == (0.002, 0.1)


def test_set_shape_on_a_title_is_refused_not_ignored():
    with pytest.raises(OpError, match="not a shape"):
        apply(_doc(), {"op": "set_shape", "layer_id": TEXT_ID,
                       "fill": "#101820"})


def test_set_shape_naming_no_fields_is_refused():
    with pytest.raises(OpError, match="names nothing to set"):
        apply(_vector_doc(), {"op": "set_shape", "layer_id": SHAPE_ID})


def test_set_shape_is_refused_on_a_locked_panel():
    doc = _vector_doc()
    apply(doc, {"op": "set_layer", "layer_id": SHAPE_ID, "locked": True})
    with pytest.raises(OpError, match="is locked"):
        apply(doc, {"op": "set_shape", "layer_id": SHAPE_ID, "radius": 0.2})


def test_clearing_the_only_paint_a_shape_has_is_refused():
    doc = _vector_doc()
    with pytest.raises(OpError, match="draws nothing"):
        apply(doc, {"op": "set_shape", "layer_id": SHAPE_ID, "fill": None})
    assert doc.layer(SHAPE_ID).fill == "#101820"


def test_a_typed_op_keeps_the_layer_where_it_is_rather_than_rebuilding_it():
    # This is why the typed ops exist: remove+add would mint a new position
    # (and, in the UI, a new id) just to change a colour.
    doc = _vector_doc()
    apply(doc, {"op": "set_scrim", "layer_id": SCRIM_ID, "color": "#0b0f14"})
    apply(doc, {"op": "set_shape", "layer_id": SHAPE_ID, "shape": "ellipse"})
    assert [l.id for l in doc.layers] == [SCRIM_ID, FRAME_ID, SHAPE_ID]
    assert doc.layer(SCRIM_ID).name == "title scrim"


def test_a_field_from_another_kinds_op_is_refused():
    with pytest.raises(OpError, match="preset"):
        apply(_vector_doc(), {"op": "set_shape", "layer_id": SHAPE_ID,
                              "preset": "single_rule"})


# -- effects ------------------------------------------------------------------

def test_set_effects_replaces_the_whole_stack():
    doc = _doc()
    apply(doc, {"op": "set_effects", "layer_id": TEXT_ID,
                "effects": [{"type": "drop_shadow", "params": {"dy": 0.004}},
                            {"type": "drop_shadow", "params": {"blur": 0.02}}]})
    apply(doc, {"op": "set_effects", "layer_id": TEXT_ID,
                "effects": [{"type": "bevel"}]})
    effects = doc.layer(TEXT_ID).effects
    assert [e.type for e in effects] == ["bevel"]


def test_set_effects_accepts_parameters_this_build_has_never_heard_of():
    doc = _doc()
    apply(doc, {"op": "set_effects", "layer_id": TEXT_ID,
                "effects": [{"type": "contact_shadow",
                             "params": {"spread": 3, "invented_next_week": True}}]})
    assert doc.layer(TEXT_ID).effects[0].params["invented_next_week"] is True


def test_set_effects_with_no_effects_key_is_refused():
    with pytest.raises(OpError, match="needs an `effects` list"):
        apply(_doc(), {"op": "set_effects", "layer_id": TEXT_ID})


def test_an_effect_with_no_type_is_refused():
    with pytest.raises(OpError, match="invalid"):
        apply(_doc(), {"op": "set_effects", "layer_id": TEXT_ID,
                       "effects": [{"params": {"depth": 1}}]})


# -- add / remove / reorder ---------------------------------------------------

def test_add_layer_with_no_index_lands_on_top():
    doc = _doc()
    layer = _new_shape()
    apply(doc, {"op": "add_layer", "layer": layer})
    assert doc.layers[-1].id == layer["id"]


def test_add_layer_at_an_index_lands_there():
    doc = _doc()
    layer = _new_shape()
    apply(doc, {"op": "add_layer", "layer": layer, "index": 1})
    assert [l.id for l in doc.layers] == [ART_ID, layer["id"], TEXT_ID, SHAPE_ID]


def test_add_layer_may_land_at_the_very_bottom():
    doc = _doc()
    layer = _new_shape()
    apply(doc, {"op": "add_layer", "layer": layer, "index": 0})
    assert doc.layers[0].id == layer["id"]


def test_add_layer_out_of_range_is_refused_never_clamped():
    with pytest.raises(OpError, match="out of range"):
        apply(_doc(), {"op": "add_layer", "layer": _new_shape(), "index": 12})


def test_add_layer_refuses_a_duplicate_id():
    with pytest.raises(OpError, match="already in this canvas"):
        apply(_doc(), {"op": "add_layer", "layer": _new_shape(id=TEXT_ID)})


def test_add_layer_needs_a_real_layer_object():
    with pytest.raises(OpError, match="needs a `layer` object"):
        apply(_doc(), {"op": "add_layer", "layer": "a scrim, please"})


def test_add_layer_reports_what_is_wrong_with_the_layer():
    with pytest.raises(OpError, match="does not validate"):
        apply(_doc(), {"op": "add_layer", "layer": _new_shape(fill="crimson")})


def test_add_layer_requires_its_own_id_so_the_op_stays_replayable():
    layer = _new_shape()
    del layer["id"]
    with pytest.raises(OpError, match="does not validate"):
        apply(_doc(), {"op": "add_layer", "layer": layer})


def test_remove_layer_removes_exactly_one():
    doc = _doc()
    apply(doc, {"op": "remove_layer", "layer_id": TEXT_ID})
    assert [l.id for l in doc.layers] == [ART_ID, SHAPE_ID]


def test_reorder_layer_moves_rather_than_swaps():
    doc = _doc()
    apply(doc, {"op": "reorder_layer", "layer_id": ART_ID, "index": 2})
    assert [l.id for l in doc.layers] == [TEXT_ID, SHAPE_ID, ART_ID]


def test_reorder_layer_past_the_end_is_refused():
    with pytest.raises(OpError, match="out of range"):
        apply(_doc(), {"op": "reorder_layer", "layer_id": ART_ID, "index": 3})


def test_reorder_layer_needs_an_index():
    with pytest.raises(OpError, match="needs an `index`"):
        apply(_doc(), {"op": "reorder_layer", "layer_id": ART_ID})


# -- history ------------------------------------------------------------------

def test_every_applied_op_is_appended_in_order():
    doc = _doc()
    apply(doc, {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2})
    apply(doc, {"op": "set_layer", "layer_id": ART_ID, "opacity": 0.5})
    assert [entry["op"] for entry in doc.history] == ["nudge", "set_layer"]
    assert doc.history[0]["dx"] == -0.2


def test_a_refused_op_records_nothing():
    doc = _doc()
    with pytest.raises(OpError):
        apply(doc, {"op": "set_text", "layer_id": ART_ID, "text": "no"})
    assert doc.history == []


def test_history_is_a_copy_so_a_caller_cannot_rewrite_the_past():
    doc = _doc()
    op = {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2}
    apply(doc, op)
    op["dx"] = 999.0
    assert doc.history[0]["dx"] == -0.2


# -- apply_many ---------------------------------------------------------------

def test_a_whole_batch_lands_together():
    doc = _doc()
    apply_many(doc, [
        {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2},
        {"op": "set_text", "layer_id": TEXT_ID, "size": 0.1},
        {"op": "add_layer", "layer": _new_shape()},
    ])
    assert doc.layer(TEXT_ID).frame.x == pytest.approx(0.3)
    assert doc.layer(TEXT_ID).size == 0.1
    assert len(doc.layers) == 4
    assert len(doc.history) == 3


def test_a_batch_that_fails_late_applies_none_of_itself():
    doc = _doc()
    before = doc.model_dump()
    with pytest.raises(OpError, match="op 3 of 3"):
        apply_many(doc, [
            {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2},
            {"op": "set_layer", "layer_id": ART_ID, "opacity": 0.5},
            {"op": "set_text", "layer_id": ART_ID, "text": "wrong kind"},
        ])
    assert doc.model_dump() == before
    assert doc.history == []


def test_a_failed_batch_names_the_op_that_failed_and_why():
    with pytest.raises(OpError) as excinfo:
        apply_many(_doc(), [{"op": "remove_layer", "layer_id": TEXT_ID},
                            {"op": "nudge", "layer_id": TEXT_ID, "dx": 0.1}])
    message = str(excinfo.value)
    assert "op 2 of 2" in message
    assert TEXT_ID in message


def test_a_batch_sees_its_own_earlier_ops():
    doc = _doc()
    added = _new_shape()
    apply_many(doc, [{"op": "add_layer", "layer": added},
                     {"op": "nudge", "layer_id": added["id"], "dx": 0.1}])
    assert doc.layer(added["id"]).frame.x == pytest.approx(0.6)


def test_an_empty_batch_changes_nothing():
    doc = _doc()
    apply_many(doc, [])
    assert doc.history == []
    assert len(doc.layers) == 3


def test_apply_many_wants_a_list():
    with pytest.raises(OpError, match="takes a list"):
        apply_many(_doc(), {"op": "nudge", "layer_id": TEXT_ID, "dx": 0.1})


def test_a_batch_preserves_layers_that_are_not_plain_models():
    # The all-or-nothing swap goes through a deep copy; a scrim's nested
    # gradient must survive it intact.
    scrim = ScrimLayer(id="ly_ddd444", frame=Frame(x=0.5, y=0.8, w=1.0, h=0.4),
                       color="#000000",
                       gradient=Gradient(angle=90.0,
                                         stops=[Stop(at=0.0, alpha=0.0),
                                                Stop(at=1.0, alpha=0.35)]))
    doc = _doc(layers=[scrim])
    apply_many(doc, [{"op": "set_layer", "layer_id": "ly_ddd444",
                      "opacity": 0.9}])
    assert doc.layer("ly_ddd444").gradient.stops[1].alpha == 0.35


# =============================================================================
# M3: the print wrap -- docproof/canvas/wrap.py and the set_wrap op
# =============================================================================
#
# Both halves of one feature live here: `to_wrap` makes the wrap and
# `set_wrap` re-measures it, and they share one arithmetic (the panel
# geometry on Wrap). Testing them apart would mean two fixtures for one
# sheet.
#
# The fixture is a 6x9 paperback with a 0.75in spine and the default 0.125in
# bleed, so the sheet is 13 x 9.25in -- 3900 x 2775px at 300dpi -- and every
# number below can be checked against those by hand.

TRIM_W, TRIM_H, SPINE, BLEED = 6.0, 9.0, 0.75, 0.125
SHEET_W, SHEET_H = 13.0, 9.25
FRONT_X0 = BLEED + TRIM_W + SPINE                       # 6.875in


def _wrap(**overrides) -> Wrap:
    data = dict(trim_w_in=TRIM_W, trim_h_in=TRIM_H, spine_in=SPINE)
    data.update(overrides)
    return Wrap(**data)


def _wrapped(**overrides) -> CanvasDoc:
    """The three-layer front cover, converted."""
    return to_wrap(_doc(), _wrap(**overrides))


def _named(doc: CanvasDoc, name: str):
    layer, = [l for l in doc.layers if l.name == name]
    return layer


def _on_panel(doc: CanvasDoc, layer_id: str, panel: str = "front") -> float:
    """A layer's center as a fraction of ONE panel -- 0.5 is centered on it.

    The unit the remap promises to preserve: the sheet grows and the panels
    move, but where a layer sits ON its panel does not change."""
    sheet_w, _ = doc.wrap.sheet_in
    x0, x1 = doc.wrap.panel_edges_in()[panel]
    return (doc.layer(layer_id).frame.x * sheet_w - x0) / (x1 - x0)


def _inches_wide(doc: CanvasDoc, layer_id: str) -> float:
    return doc.layer(layer_id).frame.w * doc.wrap.sheet_in[0]


# -- to_wrap: the front cover lands on the front panel -------------------------

def test_the_wrapped_canvas_is_the_whole_sheet():
    doc = _wrapped()
    assert (doc.canvas.w, doc.canvas.h) == (3900, 2775)
    assert doc.wrap == _wrap()


def test_a_layer_centered_on_the_front_cover_lands_centered_on_the_front_panel():
    # The exact arithmetic, not an approximation: the title's center was
    # x=0.5 of a front-only canvas, so it is now (front_x0 + 0.5 * trim_w)
    # inches from the sheet's left edge.
    doc = _wrapped()
    assert doc.layer(TEXT_ID).frame.x == pytest.approx(
        (FRONT_X0 + 0.5 * TRIM_W) / SHEET_W)
    assert doc.layer(TEXT_ID).frame.y == pytest.approx(
        (BLEED + 0.7 * TRIM_H) / SHEET_H)
    assert _on_panel(doc, TEXT_ID) == pytest.approx(0.5)


def test_a_full_cover_plate_becomes_exactly_the_front_panel():
    doc = _wrapped()
    frame = doc.layer(ART_ID).frame
    assert frame.w == pytest.approx(TRIM_W / SHEET_W)
    assert frame.h == pytest.approx(TRIM_H / SHEET_H)
    # ...which is the front panel's trim rectangle, edge for edge.
    assert frame.x - frame.w / 2 == pytest.approx(FRONT_X0 / SHEET_W)
    assert frame.y - frame.h / 2 == pytest.approx(BLEED / SHEET_H)


def test_a_corner_pin_is_remapped_with_the_box_it_refines():
    front = _doc()
    front.layer(ART_ID).frame.corners = PIN
    doc = to_wrap(front, _wrap())
    assert doc.layer(ART_ID).frame.corners == [
        [pytest.approx((FRONT_X0 + x * TRIM_W) / SHEET_W),
         pytest.approx((BLEED + y * TRIM_H) / SHEET_H)] for x, y in PIN]


def test_the_ground_is_bottom_most_and_covers_the_whole_sheet():
    doc = _wrapped()
    ground = doc.layers[0]
    assert ground.name == "wrap sheet"
    assert (ground.frame.x, ground.frame.y) == (0.5, 0.5)
    assert (ground.frame.w, ground.frame.h) == (1.0, 1.0)


def test_the_ground_takes_the_specs_own_palette_when_there_is_one():
    doc = to_wrap(_doc(source_spec={"palette": {"background": "#101820"}}),
                  _wrap())
    assert doc.layers[0].fill == "#101820"


def test_the_ground_samples_the_field_plate_when_there_is_no_palette(tmp_path):
    # No palette means the only evidence is pixels, so the bottom-most
    # plate's mean is measured rather than guessed.
    from PIL import Image
    (tmp_path / "assets").mkdir()
    Image.new("RGB", (4, 4), (16, 24, 32)).save(
        tmp_path / "assets" / "c0_background.png")
    doc = to_wrap(_doc(), _wrap(), job_dir=tmp_path)
    assert doc.layers[0].fill == "#101820"


def test_the_ground_falls_back_to_a_neutral_when_nothing_says():
    bare = _doc(layers=[_doc().layer(ART_ID)])
    doc = to_wrap(bare, _wrap())
    assert doc.layers[0].fill == "#808080"


def test_the_back_panel_gets_a_placeholder_inside_the_safe_margin():
    doc = _wrapped()
    back = _named(doc, "back cover copy")
    assert back.text == "Back cover copy…"
    left = (back.frame.x - back.frame.w / 2) * SHEET_W
    assert left == pytest.approx(BLEED + 0.25)          # SAFE_MARGIN_IN
    assert (back.frame.y - back.frame.h / 2) * SHEET_H == pytest.approx(
        BLEED + 0.25)


def test_the_spine_title_is_rotated_and_centered_on_the_spine():
    doc = _wrapped()
    spine = _named(doc, "spine title")
    assert spine.frame.rotation == 90.0
    x0, x1 = _wrap().panel_edges_in()["spine"]
    assert spine.frame.x == pytest.approx((x0 + x1) / 2 / SHEET_W)
    # The type has to fit ACROSS the spine, which is what its size is
    # stated against: 0.45 of a 0.75in spine.
    assert spine.frame.h * SHEET_H < SPINE


def test_the_spine_title_is_the_front_title_on_one_line():
    doc = _wrapped()
    spine = _named(doc, "spine title")
    assert spine.text == "THE LIGHTHOUSE AT GULL POINT"
    assert spine.family == _doc().layer(TEXT_ID).family


def test_one_text_layer_seeds_a_spine_title_and_no_author():
    # Guessing an author from the title would be a wrong answer that has to
    # be noticed before it can be fixed.
    doc = _wrapped()
    assert [l.name for l in doc.layers if l.name.startswith("spine")] == [
        "spine title"]


def test_two_text_layers_seed_both_spine_lines():
    front = _doc()
    front.layers.append(TextLayer(
        id="ly_fff666", name="author", frame=Frame(x=0.5, y=0.9, w=0.5, h=0.04),
        text="J. R. Vance", family="Spectral", size=0.03, color="#f5f1e8"))
    doc = to_wrap(front, _wrap())
    assert [l.name for l in doc.layers if l.name.startswith("spine")] == [
        "spine title", "spine author"]
    assert _named(doc, "spine author").text == "J. R. Vance"
    assert _named(doc, "spine author").size < _named(doc, "spine title").size


def test_a_document_with_no_text_leaves_the_spine_empty():
    doc = to_wrap(_doc(layers=[_doc().layer(ART_ID)]), _wrap())
    assert [l for l in doc.layers if l.name.startswith("spine")] == []


def test_the_conversion_records_the_moment_the_doc_became_a_wrap():
    doc = _wrapped()
    record = doc.history[-1]
    assert record["op"] == "to_wrap"
    assert record["wrap"]["spine_in"] == SPINE
    assert record["canvas"] == {"w": 3900, "h": 2775}
    # The seeded ids, so a reader can tell the new panels from the cover's
    # own layers without guessing from names.
    seeded = set(record["seeded"])
    assert {l.id for l in doc.layers} - seeded == {ART_ID, TEXT_ID, SHAPE_ID}


def test_converting_a_wrap_again_is_refused_with_the_reason():
    with pytest.raises(WrapError, match="already a wrap"):
        to_wrap(_wrapped(), _wrap(spine_in=1.0))


def test_the_conversion_leaves_the_document_it_was_given_alone():
    front = _doc()
    to_wrap(front, _wrap())
    assert front.wrap is None
    assert front.layer(TEXT_ID).frame.x == 0.5
    assert len(front.layers) == 3


# -- panels(): one answer, for the route and the guides ------------------------

def test_the_panel_ranges_meet_edge_to_edge_and_fill_the_sheet():
    p = panels(_wrap())
    assert p["back"]["x0"] == pytest.approx(p["bleed"]["x"])
    assert p["back"]["x1"] == pytest.approx(p["spine"]["x0"])
    assert p["spine"]["x1"] == pytest.approx(p["front"]["x0"])
    assert p["front"]["x1"] == pytest.approx(1.0 - p["bleed"]["x"])
    widths = sum(p[name]["x1"] - p[name]["x0"]
                 for name in ("back", "spine", "front"))
    assert widths + 2 * p["bleed"]["x"] == pytest.approx(1.0)


def test_every_panel_shares_the_trims_own_top_and_bottom():
    p = panels(_wrap())
    for name in ("back", "spine", "front"):
        assert p[name]["y0"] == pytest.approx(p["bleed"]["y"])
        assert p[name]["y1"] == pytest.approx(1.0 - p["bleed"]["y"])


def test_panels_carries_the_sheet_in_both_units_and_the_safe_inset():
    p = panels(_wrap())
    assert p["sheet"] == {"w_in": SHEET_W, "h_in": SHEET_H, "w_px": 3900,
                          "h_px": 2775, "dpi": 300}
    assert p["safe"]["inches"] == 0.25
    assert p["safe"]["x"] == pytest.approx(0.25 / SHEET_W)
    assert p["safe"]["y"] == pytest.approx(0.25 / SHEET_H)


# -- set_wrap: the spine firms up ---------------------------------------------

def test_set_wrap_on_a_front_cover_is_refused():
    with pytest.raises(OpError, match="still a front cover"):
        apply(_doc(), {"op": "set_wrap", "spine_in": 1.0})


@pytest.mark.parametrize("field", ["trim_w_in", "trim_h_in"])
def test_set_wrap_refuses_to_change_the_trim_and_says_why(field):
    with pytest.raises(OpError, match="a different book"):
        apply(_wrapped(), {"op": "set_wrap", field: 5.5})


def test_a_wider_spine_grows_the_sheet_and_moves_nothing_on_its_panel():
    doc = _wrapped()
    before = {"front": _on_panel(doc, TEXT_ID), "art": _on_panel(doc, ART_ID),
              "inches": _inches_wide(doc, TEXT_ID)}
    apply(doc, {"op": "set_wrap", "spine_in": 1.25})

    assert doc.wrap.spine_in == 1.25
    assert (doc.canvas.w, doc.canvas.h) == (round(13.5 * 300), 2775)
    assert _on_panel(doc, TEXT_ID) == pytest.approx(before["front"])
    assert _on_panel(doc, ART_ID) == pytest.approx(before["art"])
    # Sizes are preserved in INCHES; only the fraction changes, because the
    # sheet did.
    assert _inches_wide(doc, TEXT_ID) == pytest.approx(before["inches"])
    assert doc.layer(TEXT_ID).frame.w == pytest.approx(before["inches"] / 13.5)


def test_a_wider_spine_leaves_the_back_panel_where_it_was():
    doc = _wrapped()
    back_id = _named(doc, "back cover copy").id
    before = _on_panel(doc, back_id, "back")
    apply(doc, {"op": "set_wrap", "spine_in": 1.25})
    assert _on_panel(doc, back_id, "back") == pytest.approx(before)


def test_spine_type_stays_centered_on_a_spine_that_widened():
    doc = _wrapped()
    apply(doc, {"op": "set_wrap", "spine_in": 1.25})
    assert _on_panel(doc, _named(doc, "spine title").id, "spine") == \
        pytest.approx(0.5)


def test_the_ground_still_covers_a_sheet_that_grew():
    # The one exception to "keep your inches": a layer that IS the sheet
    # keeps its fractions, or the wrap prints with a bare strip down one
    # edge.
    doc = _wrapped()
    apply(doc, {"op": "set_wrap", "spine_in": 1.25, "bleed_in": 0.25})
    ground = doc.layers[0]
    assert (ground.frame.w, ground.frame.h) == (1.0, 1.0)
    assert (ground.frame.x, ground.frame.y) == (0.5, 0.5)


def test_more_bleed_keeps_every_layer_where_the_trim_is():
    doc = _wrapped()
    sheet_h_before = doc.wrap.sheet_in[1]
    top_before = doc.layer(TEXT_ID).frame.y * sheet_h_before - BLEED
    apply(doc, {"op": "set_wrap", "bleed_in": 0.25})

    sheet_h = doc.wrap.sheet_in[1]
    assert sheet_h == pytest.approx(TRIM_H + 0.5)
    assert doc.layer(TEXT_ID).frame.y * sheet_h - 0.25 == pytest.approx(
        top_before)
    assert _on_panel(doc, TEXT_ID) == pytest.approx(0.5)


def test_a_dpi_change_moves_nothing_at_all():
    doc = _wrapped()
    before = [(l.frame.x, l.frame.y, l.frame.w, l.frame.h) for l in doc.layers]
    apply(doc, {"op": "set_wrap", "dpi": 600})
    assert [(l.frame.x, l.frame.y, l.frame.w, l.frame.h)
            for l in doc.layers] == before
    assert (doc.canvas.w, doc.canvas.h) == (7800, 5550)


def test_set_wrap_remaps_a_corner_pin_with_its_layer():
    doc = _wrapped()
    pinned = doc.layer(ART_ID)
    pinned.frame.corners = [[p, q] for p, q in
                            [(0.55, 0.1), (0.95, 0.1), (0.95, 0.9), (0.55, 0.9)]]
    apply(doc, {"op": "set_wrap", "spine_in": 1.25})
    shift = (0.125 + TRIM_W + 1.25) - FRONT_X0                  # the front moved
    assert doc.layer(ART_ID).frame.corners[0][0] == pytest.approx(
        (0.55 * SHEET_W + shift) / 13.5)


def test_set_wrap_moves_locked_layers_too():
    # Locking stops a drag from nudging the background; re-measuring the
    # sheet is not a drag.
    doc = _wrapped()
    doc.layer(ART_ID).locked = True
    before = _on_panel(doc, ART_ID)
    apply(doc, {"op": "set_wrap", "spine_in": 1.25})
    assert _on_panel(doc, ART_ID) == pytest.approx(before)


def test_set_wrap_names_a_layer_at_its_peril():
    with pytest.raises(OpError, match="does not take"):
        apply(_wrapped(), {"op": "set_wrap", "layer_id": ART_ID,
                           "spine_in": 1.0})


def test_set_wrap_that_names_nothing_to_set_is_refused():
    with pytest.raises(OpError, match="names nothing to set"):
        apply(_wrapped(), {"op": "set_wrap"})


@pytest.mark.parametrize("bad", [{"spine_in": 0.0}, {"dpi": 5000},
                                 {"bleed_in": -0.125}])
def test_set_wrap_refuses_numbers_that_are_not_a_book(bad):
    with pytest.raises(OpError, match="would leave the wrap invalid"):
        apply(_wrapped(), {"op": "set_wrap", **bad})


def test_a_refused_set_wrap_changes_nothing():
    doc = _wrapped()
    before = doc.model_dump(mode="json")
    with pytest.raises(OpError):
        apply(doc, {"op": "set_wrap", "spine_in": -1.0})
    assert doc.model_dump(mode="json") == before


def test_set_wrap_is_recorded_in_the_history_like_every_other_op():
    doc = _wrapped()
    apply(doc, {"op": "set_wrap", "spine_in": 1.25})
    assert doc.history[-1] == {"op": "set_wrap", "spine_in": 1.25}


def test_a_batch_carries_the_new_sheet_back_out_of_the_draft():
    # apply_many swaps a deep copy in; set_wrap is the one op that changes
    # more than layers and history, so the swap has to carry those too.
    doc = _wrapped()
    apply_many(doc, [{"op": "set_wrap", "spine_in": 1.25},
                     {"op": "nudge", "layer_id": TEXT_ID, "dy": 0.01}])
    assert doc.wrap.spine_in == 1.25
    assert doc.canvas.w == round(13.5 * 300)
    assert doc.model_dump(mode="json")                  # still a valid document


def test_a_wrapped_document_survives_the_round_trip_through_pydantic():
    doc = _wrapped()
    apply(doc, {"op": "set_wrap", "spine_in": 1.0625, "bleed_in": 0.1875})
    reloaded = CanvasDoc.model_validate(doc.model_dump(mode="json"))
    assert reloaded.wrap == doc.wrap
    assert (reloaded.canvas.w, reloaded.canvas.h) == (doc.canvas.w,
                                                      doc.canvas.h)


def test_the_ground_never_takes_a_titles_ink_for_the_field():
    # A text layer's color is the ink that has to READ against the field --
    # the one color the cover guarantees is wrong for the field itself.
    front = _doc(layers=[_doc().layer(ART_ID), _doc().layer(TEXT_ID)])
    assert to_wrap(front, _wrap()).layers[0].fill == "#808080"


def test_the_ground_takes_a_procedural_fields_scrim_when_there_is_one():
    # What ingest leaves behind for a field with no plate on disk.
    front = _doc(layers=[
        ScrimLayer(id=SCRIM_ID, name="background",
                   frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0), color="#2b1d14",
                   gradient=Gradient(stops=[Stop(at=0.0, alpha=1.0),
                                            Stop(at=1.0, alpha=1.0)])),
        _doc().layer(TEXT_ID)])
    assert to_wrap(front, _wrap()).layers[0].fill == "#2b1d14"


# -- masks and adjust layers (§15.2 / §15.3) ----------------------------------

def _adjust(**overrides) -> dict:
    data = dict(id=new_layer_id(), kind="adjust", name="grade", op="grade",
                frame={"x": 0.5, "y": 0.5, "w": 1.0, "h": 1.0})
    data.update(overrides)
    return data


def test_set_mask_windows_a_layer_through_one_below_it():
    doc = _doc()
    below, above = doc.layers[0].id, doc.layers[-1].id
    apply(doc, {"op": "set_mask", "layer_id": above,
                "mask": {"from_layer": below}})
    assert doc.layers[-1].mask.from_layer == below


def test_set_mask_with_null_removes_one():
    doc = _doc()
    below, above = doc.layers[0].id, doc.layers[-1].id
    apply(doc, {"op": "set_mask", "layer_id": above,
                "mask": {"from_layer": below}})
    apply(doc, {"op": "set_mask", "layer_id": above, "mask": None})
    assert doc.layers[-1].mask is None


def test_set_mask_needs_the_field_so_doing_nothing_is_never_silent():
    """An absent `mask` key would be indistinguishable from asking for
    nothing, and silently doing nothing is how a windowed plate ships
    full-bleed."""
    doc = _doc()
    with pytest.raises(OpError, match="needs a `mask`"):
        apply(doc, {"op": "set_mask", "layer_id": doc.layers[0].id})


def test_a_mask_may_not_name_a_layer_above_the_one_wearing_it():
    """Both renderers draw the document in one bottom-to-top pass, so a
    mask's source has to be already drawn. The rule is what makes a cycle
    unrepresentable rather than merely rejected."""
    doc = _doc()
    below, above = doc.layers[0].id, doc.layers[-1].id
    with pytest.raises(OpError, match="not a layer below it"):
        apply(doc, {"op": "set_mask", "layer_id": below,
                    "mask": {"from_layer": above}})


def test_reordering_a_layer_under_its_own_mask_source_is_refused():
    """The check has to be at the DOCUMENT level: reorder_layer touches no
    mask at all and can still strand one."""
    doc = _doc()
    below, above = doc.layers[0].id, doc.layers[-1].id
    apply(doc, {"op": "set_mask", "layer_id": above,
                "mask": {"from_layer": below}})
    with pytest.raises(OpError, match="would leave the document invalid"):
        apply(doc, {"op": "reorder_layer", "layer_id": below, "index": len(doc.layers) - 1})


def test_removing_a_mask_source_is_refused_while_something_masks_through_it():
    doc = _doc()
    below, above = doc.layers[0].id, doc.layers[-1].id
    apply(doc, {"op": "set_mask", "layer_id": above,
                "mask": {"from_layer": below}})
    with pytest.raises(OpError, match="would leave the document invalid"):
        apply(doc, {"op": "remove_layer", "layer_id": below})


def test_a_batch_may_pass_through_an_arrangement_a_single_op_could_not():
    """apply_many checks once at the batch boundary, because a batch is
    atomic and the batch is the only state anyone ever sees. "Clip the plate
    into the title, and move the title under it" is the edit that needs
    it — legal at the end, illegal in the middle."""
    doc = _doc()
    title, plate = doc.layers[-1].id, doc.layers[0].id
    apply_many(doc, [
        {"op": "reorder_layer", "layer_id": title, "index": 0},
        {"op": "set_mask", "layer_id": plate, "mask": {"from_layer": title}},
    ])
    assert doc.layers[0].id == title
    assert next(l for l in doc.layers if l.id == plate).mask.from_layer == title


def test_a_refused_batch_leaves_the_document_exactly_as_it_was():
    doc = _doc()
    before = doc.model_dump()
    with pytest.raises(OpError, match="would leave the document invalid"):
        apply_many(doc, [{"op": "set_mask", "layer_id": doc.layers[0].id,
                          "mask": {"from_layer": doc.layers[-1].id}}])
    assert doc.model_dump() == before


def test_set_adjust_edits_a_grade_in_place():
    doc = _doc()
    apply(doc, {"op": "add_layer", "layer": _adjust(id="ly_grade000")})
    apply(doc, {"op": "set_adjust", "layer_id": "ly_grade000",
                "brightness": -0.2, "contrast": 0.1})
    layer = next(l for l in doc.layers if l.id == "ly_grade000")
    assert (layer.brightness, layer.contrast) == (-0.2, 0.1)


def test_set_adjust_says_op_kind_because_op_is_already_taken():
    """The one place a wire name differs from the model's: an op dict cannot
    carry two meanings of `op`. Changing the adjustment must not cost the
    layer its id or its place in the stack."""
    doc = _doc()
    apply(doc, {"op": "add_layer", "layer": _adjust(id="ly_grade000")})
    apply(doc, {"op": "set_adjust", "layer_id": "ly_grade000",
                "op_kind": "vignette", "strength": 0.4})
    layer = next(l for l in doc.layers if l.id == "ly_grade000")
    assert (layer.op, layer.strength) == ("vignette", 0.4)
    assert doc.layers[-1].id == "ly_grade000"


def test_set_adjust_refuses_a_layer_that_is_not_an_adjustment():
    doc = _doc()
    with pytest.raises(OpError, match="an adjust layer"):
        apply(doc, {"op": "set_adjust", "layer_id": doc.layers[0].id,
                    "brightness": 0.1})
