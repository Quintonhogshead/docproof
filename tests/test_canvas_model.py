"""docproof/canvas/model.py: the editor document's own validation edges.

Everything here is pure pydantic plus one atomic file write — no cover job,
no pixels, no network. The point of these tests is the promise the module
makes to the browser and to the assistant on the other end of the wire: a
malformed layer fails at validation with a sentence, never as a layer that
silently ignored half of what it was told.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from docproof.canvas.model import (DOC_VERSION, ArtLayer, CanvasDoc, Effect,
                                   Frame, FrameLayer, Gradient, LayerBase,
                                   PlateVersion, ScrimLayer, ShapeLayer, Size,
                                   Stop, TextLayer, Warp, Wrap, load_doc,
                                   new_layer_id, parse_layer, save_doc)


def _frame(**overrides) -> Frame:
    data = dict(x=0.5, y=0.5, w=0.8, h=0.2)
    data.update(overrides)
    return Frame(**data)


def _text_layer(**overrides) -> TextLayer:
    data = dict(id=new_layer_id(), name="title", frame=_frame(),
                text="THE LIGHTHOUSE", family="Playfair Display",
                size=0.08, color="#f5f1e8")
    data.update(overrides)
    return TextLayer(**data)


def _art_layer(**overrides) -> ArtLayer:
    data = dict(id=new_layer_id(), name="background",
                frame=_frame(x=0.5, y=0.5, w=1.0, h=1.0),
                source="assets/c0_background.png",
                prompt="A smoky brass foundry at dusk.", transparent=False)
    data.update(overrides)
    return ArtLayer(**data)


def _scrim_layer(**overrides) -> ScrimLayer:
    data = dict(id=new_layer_id(), frame=_frame(), color="#000000",
                gradient=Gradient(stops=[Stop(at=0.0, alpha=0.0),
                                         Stop(at=1.0, alpha=0.4)]))
    data.update(overrides)
    return ScrimLayer(**data)


def _doc(**overrides) -> CanvasDoc:
    data = dict(job_id="20260831T120000Z-a1b2c3",
                canvas=Size(w=1600, h=2560),
                layers=[_art_layer(), _text_layer()])
    data.update(overrides)
    return CanvasDoc(**data)


# -- extra="forbid" everywhere but Effect -------------------------------------

_STRICT_MODELS = [Size, Wrap, Frame, Warp, Stop, Gradient, PlateVersion,
                  LayerBase, ArtLayer, TextLayer, ScrimLayer, FrameLayer,
                  ShapeLayer, CanvasDoc]


@pytest.mark.parametrize("model_cls", _STRICT_MODELS, ids=lambda c: c.__name__)
def test_every_model_but_effect_forbids_extra_fields(model_cls):
    assert model_cls.model_config.get("extra") == "forbid"


def test_effect_is_the_one_model_that_allows_extra_fields():
    # Effects are drawn client-side and their vocabulary grows faster than a
    # server-side schema should churn -- see the model's own docstring.
    assert Effect.model_config.get("extra") == "allow"
    effect = Effect(type="bevel", params={"depth": 0.4}, softness=2)
    assert effect.params["depth"] == 0.4


def test_a_stray_field_on_a_layer_fails_at_runtime_not_at_render_time():
    with pytest.raises(ValidationError):
        _text_layer(valign="middle")


# -- ids ----------------------------------------------------------------------

def test_new_layer_id_is_ly_plus_six_hex_characters():
    ident = new_layer_id()
    assert ident.startswith("ly_")
    assert len(ident) == 9
    int(ident[3:], 16)            # raises if it is not hex


def test_new_layer_id_does_not_repeat_itself():
    assert len({new_layer_id() for _ in range(200)}) == 200


def test_two_layers_with_the_same_id_are_refused():
    shared = new_layer_id()
    with pytest.raises(ValidationError, match="share the id"):
        _doc(layers=[_art_layer(id=shared), _text_layer(id=shared)])


# -- Frame --------------------------------------------------------------------

def test_a_layer_may_hang_off_the_canvas_on_purpose():
    frame = _frame(x=-0.3, y=1.4)
    assert (frame.x, frame.y) == (-0.3, 1.4)


def test_a_center_in_another_postcode_is_still_refused():
    with pytest.raises(ValidationError):
        _frame(x=9.0)


@pytest.mark.parametrize("field", ["w", "h"])
def test_a_zero_sized_box_is_refused(field):
    with pytest.raises(ValidationError):
        _frame(**{field: 0.0})


def test_rotation_and_flips_default_to_untransformed():
    frame = _frame()
    assert (frame.rotation, frame.flip_h, frame.flip_v) == (0.0, False, False)


# -- the corner pin -----------------------------------------------------------

PIN = [[0.08, 0.12], [0.94, 0.05], [0.97, 0.88], [0.11, 0.95]]


def test_a_frame_is_unpinned_by_default():
    assert _frame().corners is None


def test_four_points_in_tl_tr_br_bl_order_are_accepted():
    frame = _frame(corners=PIN)
    assert frame.corners == PIN


def test_the_pin_does_not_disturb_the_box_it_refines():
    # x/y/w/h stay authoritative for where the layer IS -- corners only say
    # how the pixels sit inside it.
    frame = _frame(x=0.4, y=0.6, w=0.5, h=0.3, corners=PIN)
    assert (frame.x, frame.y, frame.w, frame.h) == (0.4, 0.6, 0.5, 0.3)


@pytest.mark.parametrize("bad", [
    [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],                       # a triangle
    [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9], [0.5, 0.5]],
    [],
], ids=["three", "five", "none"])
def test_a_pin_that_is_not_four_points_is_refused(bad):
    with pytest.raises(ValidationError, match="exactly 4 points"):
        _frame(corners=bad)


def test_a_corner_that_is_not_an_xy_pair_is_refused():
    with pytest.raises(ValidationError, match=r"\[x, y\] pair"):
        _frame(corners=[[0.1, 0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])


def test_a_corner_in_another_postcode_is_refused_like_the_center_is():
    with pytest.raises(ValidationError, match="outside the -2 to 2"):
        _frame(corners=[[0.1, 0.1], [9.0, 0.1], [0.9, 0.9], [0.1, 0.9]])


def test_a_pinned_corner_may_hang_off_the_trim():
    assert _frame(corners=[[-0.2, -0.1], [1.2, 0.0],
                           [1.1, 1.05], [0.05, 0.9]]).corners[0] == [-0.2, -0.1]


def test_a_corner_that_is_not_a_number_is_refused():
    with pytest.raises(ValidationError):
        _frame(corners=[["left", "top"], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])


def test_the_pin_round_trips_through_save_and_load(tmp_path):
    doc = _doc(layers=[_art_layer(frame=_frame(w=1.0, h=1.0, corners=PIN))])
    path = tmp_path / "canvas.json"
    save_doc(doc, path)
    assert load_doc(path).layers[0].frame.corners == PIN


def test_an_unpinned_frame_round_trips_as_null(tmp_path):
    path = tmp_path / "canvas.json"
    save_doc(_doc(), path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["layers"][0]["frame"]["corners"] is None
    assert load_doc(path).layers[0].frame.corners is None


# -- colors and fonts ---------------------------------------------------------

@pytest.mark.parametrize("bad", ["red", "#fff", "#ffffffff", "f5f1e8", ""])
def test_a_color_that_is_not_rrggbb_is_refused(bad):
    with pytest.raises(ValidationError, match="hex color"):
        _text_layer(color=bad)


def test_an_unregistered_font_family_is_refused_with_the_shelf_listed():
    with pytest.raises(ValidationError, match="not registered"):
        _text_layer(family="Comic Sans MS")


def test_a_registered_family_passes():
    assert _text_layer(family="Spectral").family == "Spectral"


# -- plate sources ------------------------------------------------------------

def test_the_pipelines_own_asset_path_shape_is_accepted():
    # docproof.cover.pipeline writes plates as assets/c<n>_<slot>.png.
    assert _art_layer(source="assets/c2_focal.png").source == "assets/c2_focal.png"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../secrets.png",
                                 "assets\\c0.png", "C:/plates/x.png", ""])
def test_a_source_that_leaves_the_job_directory_is_refused(bad):
    with pytest.raises(ValidationError):
        _art_layer(source=bad)


def test_plate_history_validates_its_sources_too():
    with pytest.raises(ValidationError):
        PlateVersion(source="/tmp/old.png", prompt="x")
    kept = PlateVersion(source="assets/c0_focal.png", prompt="a lighthouse")
    assert kept.prompt == "a lighthouse"


# -- gradients ----------------------------------------------------------------

def test_a_single_stop_is_a_constant_not_a_gradient():
    with pytest.raises(ValidationError):
        Gradient(stops=[Stop(at=0.0, alpha=0.5)])


def test_gradient_stops_must_be_in_order():
    with pytest.raises(ValidationError, match="non-decreasing"):
        Gradient(stops=[Stop(at=0.8, alpha=0.1), Stop(at=0.2, alpha=0.9)])


def test_alpha_outside_zero_to_one_is_refused():
    with pytest.raises(ValidationError):
        Stop(at=0.5, alpha=1.4)


# -- shapes and frames --------------------------------------------------------

def test_a_shape_with_neither_fill_nor_stroke_draws_nothing_and_is_refused():
    with pytest.raises(ValidationError, match="draws nothing"):
        ShapeLayer(id=new_layer_id(), frame=_frame(), shape="rect", fill=None)


def test_a_stroke_only_shape_is_fine():
    shape = ShapeLayer(id=new_layer_id(), frame=_frame(), shape="ellipse",
                       fill=None, stroke="#c9a227", stroke_w=0.004)
    assert shape.fill is None and shape.stroke == "#c9a227"


def test_an_unknown_frame_preset_is_refused():
    with pytest.raises(ValidationError):
        FrameLayer(id=new_layer_id(), frame=_frame(), preset="art_deco_swirl",
                   stroke="#c9a227", stroke_w=0.003)


def test_a_frame_layer_with_no_stroke_width_is_refused():
    with pytest.raises(ValidationError):
        FrameLayer(id=new_layer_id(), frame=_frame(), preset="single_rule",
                   stroke="#c9a227", stroke_w=0.0)


# -- the discriminated union --------------------------------------------------

def test_parse_layer_dispatches_on_kind():
    layer = parse_layer({"id": "ly_000001", "kind": "scrim",
                         "frame": {"x": 0.5, "y": 0.5, "w": 1.0, "h": 0.4},
                         "color": "#000000",
                         "gradient": {"stops": [{"at": 0.0, "alpha": 0.0},
                                                {"at": 1.0, "alpha": 0.5}]}})
    assert isinstance(layer, ScrimLayer)


def test_an_unknown_kind_is_refused_by_the_discriminator():
    with pytest.raises(ValidationError):
        parse_layer({"id": "ly_000001", "kind": "video",
                     "frame": {"x": 0.5, "y": 0.5, "w": 1.0, "h": 1.0}})


def test_the_discriminator_reports_the_right_kinds_missing_field():
    # One readable error about the art layer, not five stacked union errors.
    with pytest.raises(ValidationError) as excinfo:
        parse_layer({"id": "ly_000001", "kind": "art",
                     "frame": {"x": 0.5, "y": 0.5, "w": 1.0, "h": 1.0}})
    assert "source" in str(excinfo.value)


# -- doc.layer ----------------------------------------------------------------

def test_layer_lookup_returns_the_layer():
    doc = _doc()
    assert doc.layer(doc.layers[1].id) is doc.layers[1]


def test_layer_lookup_failure_names_what_is_actually_there():
    doc = _doc()
    with pytest.raises(KeyError) as excinfo:
        doc.layer("ly_nope00")
    message = excinfo.value.args[0]
    assert "ly_nope00" in message
    assert doc.layers[0].id in message


# -- persistence --------------------------------------------------------------

def test_save_then_load_round_trips_every_field(tmp_path):
    doc = _doc(history=[{"op": "nudge", "layer_id": "ly_000001", "dx": -0.2}],
               cost_usd=0.13,
               source_spec={"archetype": "probe_scene", "version": 3})
    path = tmp_path / "canvas.json"
    save_doc(doc, path)
    loaded = load_doc(path)
    assert loaded.model_dump() == doc.model_dump()
    assert loaded.history[0]["dx"] == -0.2
    assert loaded.cost_usd == 0.13


def test_saving_leaves_no_staging_file_behind(tmp_path):
    path = tmp_path / "canvas.json"
    save_doc(_doc(), path)
    assert [p.name for p in tmp_path.iterdir()] == ["canvas.json"]


def test_saved_json_is_indented_so_a_human_can_read_a_diff(tmp_path):
    path = tmp_path / "canvas.json"
    save_doc(_doc(), path)
    assert "\n  " in path.read_text(encoding="utf-8")


def test_source_spec_is_never_re_validated_against_the_cover_model(tmp_path):
    # A canvas document archived today must still open when CoverSpec has
    # grown three fields and forbidden two -- source_spec is provenance, not
    # a live model.
    doc = _doc(source_spec={"archetype": "gone_from_the_shelf",
                            "a_field_covermodel_would_forbid": True,
                            "palette": "not even a palette"})
    path = tmp_path / "canvas.json"
    save_doc(doc, path)
    assert load_doc(path).source_spec["a_field_covermodel_would_forbid"] is True


def test_loading_a_document_from_a_newer_build_is_refused(tmp_path):
    path = tmp_path / "canvas.json"
    raw = json.loads(_doc().model_dump_json())
    raw["version"] = DOC_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="upgrade before opening"):
        load_doc(path)


def test_loading_a_missing_file_names_it(tmp_path):
    with pytest.raises(FileNotFoundError) as excinfo:
        load_doc(tmp_path / "nothing-here.json")
    assert "nothing-here.json" in str(excinfo.value)


def test_defaults_are_the_empty_session():
    doc = CanvasDoc(job_id="j", canvas=Size(w=1600, h=2560))
    assert (doc.version, doc.layers, doc.history, doc.cost_usd,
            doc.source_spec) == (DOC_VERSION, [], [], 0.0, {})


def test_a_canvas_size_must_be_real_pixels():
    with pytest.raises(ValidationError):
        CanvasDoc(job_id="j", canvas=Size(w=0, h=2560))


def test_text_layer_units_are_documented_defaults():
    layer = _text_layer()
    assert (layer.style, layer.align, layer.tracking) == ("regular", "center", 0.0)
    assert layer.line_height == 1.1
    assert layer.warp == Warp(kind="none", amount=0.0)


def test_paths_and_frames_survive_a_json_round_trip_by_value(tmp_path):
    doc = _doc(layers=[_scrim_layer(), _art_layer(), _text_layer()])
    path = Path(tmp_path) / "canvas.json"
    save_doc(doc, path)
    kinds = [l.kind for l in load_doc(path).layers]
    assert kinds == ["scrim", "art", "text"]


# =============================================================================
# M3: the print wrap
# =============================================================================
#
# The wrap is where the fractional geometry has to pay off: the document does
# not learn a second coordinate convention, it is read against a bigger
# canvas. So these tests are about the two numbers that must agree -- the
# sheet in inches and the canvas in pixels -- and about refusing a wrap that
# is not a book.

def _wrap(**overrides) -> Wrap:
    """A 6x9 paperback with a 0.75in spine: 13 x 9.25in of sheet, which at
    300dpi is 3900 x 2775px."""
    data = dict(trim_w_in=6.0, trim_h_in=9.0, spine_in=0.75)
    data.update(overrides)
    return Wrap(**data)


def test_a_wrap_states_the_sheet_in_both_units():
    wrap = _wrap()
    assert wrap.sheet_in == (2 * 6.0 + 0.75 + 2 * 0.125, 9.0 + 2 * 0.125)
    assert (wrap.sheet_size().w, wrap.sheet_size().h) == (3900, 2775)


def test_wrap_defaults_are_the_printers_own_numbers():
    wrap = _wrap()
    assert (wrap.bleed_in, wrap.dpi) == (0.125, 300)


@pytest.mark.parametrize("bad", [
    dict(trim_w_in=0.0), dict(trim_h_in=-9.0), dict(spine_in=0.0),
    dict(bleed_in=0.0), dict(dpi=71), dict(dpi=601),
])
def test_a_wrap_refuses_dimensions_that_are_not_a_book(bad):
    with pytest.raises(ValidationError):
        _wrap(**bad)


def test_the_panels_run_back_spine_front_left_to_right():
    # The printed-wrap convention the Wrap docstring draws: fold the sheet
    # around the block and the front lands face up, so the front is right.
    edges = _wrap().panel_edges_in()
    assert edges["back"] == (0.125, 6.125)
    assert edges["spine"] == (6.125, 6.875)
    assert edges["front"] == (6.875, 12.875)
    # Every panel shares one trim: the sheet is cut once.
    assert _wrap().trim_y_in() == (0.125, 9.125)


def test_the_panels_and_the_bleed_tile_the_whole_sheet():
    wrap = _wrap()
    sheet_w, _ = wrap.sheet_in
    edges = wrap.panel_edges_in()
    assert edges["front"][1] == sheet_w - wrap.bleed_in
    widths = sum(x1 - x0 for x0, x1 in edges.values())
    assert widths + 2 * wrap.bleed_in == pytest.approx(sheet_w)


def test_a_wrapped_document_canvas_is_the_sheet():
    wrap = _wrap()
    doc = _doc(canvas=wrap.sheet_size(), wrap=wrap)
    assert doc.wrap is not None
    assert (doc.canvas.w, doc.canvas.h) == (3900, 2775)


def test_a_wrapped_document_whose_canvas_is_not_the_sheet_is_refused():
    # The failure this catches prints at the wrong physical size, which is
    # a thing you learn from the printer -- the worst place to learn it.
    with pytest.raises(ValidationError, match="the canvas IS the sheet"):
        _doc(canvas=Size(w=1600, h=2560), wrap=_wrap())


def test_a_front_only_document_still_has_no_wrap():
    assert _doc().wrap is None


def test_a_wrap_survives_the_json_round_trip(tmp_path):
    wrap = _wrap(spine_in=1.125, dpi=600)
    path = Path(tmp_path) / "canvas.json"
    save_doc(_doc(canvas=wrap.sheet_size(), wrap=wrap), path)
    assert json.loads(path.read_text())["wrap"]["spine_in"] == 1.125
    assert load_doc(path).wrap == wrap
