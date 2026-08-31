"""docproof/canvas/render.py: the browser's picture, drawn in Python.

The bar this suite holds the renderer to is STRUCTURAL parity with
app/static/canvas/engine.js, never pixel equality — PIL and Konva rasterize
differently and always will. So every assertion here is about geometry a
person would notice: which layer won where they overlap, whether a `cover`
fit cropped and a `contain` fit letterboxed, whether a warp bowed the line in
the axis its name promises, whether a shadow reached outside the box it came
from, whether the corner pin put the plate's corner on the point the document
named. Probe pixels and alpha bounding boxes, not checksums.

Every document is built in-test and every plate is generated with PIL, so the
suite depends on nothing in a job directory and nothing on the network.
"""
from __future__ import annotations

import math

import pytest
from PIL import Image

from docproof.canvas import render as R
from docproof.canvas.model import (ArtLayer, CanvasDoc, Effect, Frame,
                                   FrameLayer, Gradient, ScrimLayer,
                                   ShapeLayer, Size, Stop, TextLayer, Warp)

BLUE = (0, 0, 255, 255)
RED = (255, 0, 0, 255)
GREEN = (0, 255, 0, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)


# -- fixtures and helpers -----------------------------------------------------

@pytest.fixture
def job_dir(tmp_path):
    """A job directory with an `assets/` subdirectory, the way the cover
    pipeline writes plates (model._validate_source's own example)."""
    (tmp_path / "assets").mkdir()
    return tmp_path


def write_plate(job_dir, name: str, size=(40, 20), color=RED) -> str:
    rel = f"assets/{name}"
    Image.new("RGBA", size, color).save(job_dir / rel)
    return rel


def write_quadrant_plate(job_dir, name: str) -> str:
    """A 40x20 plate with four coloured quadrants and a black band down its
    leftmost tenth — the band is what tells a `cover` crop (which eats it)
    apart from a `stretch` (which does not)."""
    img = Image.new("RGBA", (40, 20), RED)
    img.paste(Image.new("RGBA", (20, 10), BLUE), (0, 0))
    img.paste(Image.new("RGBA", (20, 10), GREEN), (0, 10))
    img.paste(Image.new("RGBA", (20, 10), WHITE), (20, 10))
    img.paste(Image.new("RGBA", (4, 20), BLACK), (0, 0))
    img.save(job_dir / f"assets/{name}")
    return f"assets/{name}"


def doc_with(*layers, canvas=(200, 320)) -> CanvasDoc:
    return CanvasDoc(job_id="20260831T120000Z-a1b2c3",
                     canvas=Size(w=canvas[0], h=canvas[1]),
                     layers=list(layers))


def art(layer_id="ly_art", source="assets/plate.png", **kw) -> ArtLayer:
    frame = kw.pop("frame", Frame(x=0.5, y=0.5, w=1.0, h=1.0))
    return ArtLayer(id=layer_id, frame=frame, source=source, **kw)


def shape(layer_id="ly_shape", **kw) -> ShapeLayer:
    frame = kw.pop("frame", Frame(x=0.5, y=0.5, w=0.4, h=0.2))
    kw.setdefault("shape", "rect")
    kw.setdefault("fill", "#ff0000")
    return ShapeLayer(id=layer_id, frame=frame, **kw)


def text(layer_id="ly_text", **kw) -> TextLayer:
    frame = kw.pop("frame", Frame(x=0.5, y=0.5, w=0.8, h=0.3))
    kw.setdefault("text", "HELLO")
    kw.setdefault("family", "Poppins")
    kw.setdefault("size", 0.08)
    kw.setdefault("color", "#ffffff")
    return TextLayer(id=layer_id, frame=frame, **kw)


def ink_bbox(img: Image.Image):
    """Where the drawn ink actually is: the alpha channel's bounding box."""
    return img.getchannel("A").getbbox()


def alpha_at(img: Image.Image, x: int, y: int) -> int:
    return img.getpixel((x, y))[3]


# -- canvas size --------------------------------------------------------------

def test_render_is_rgba_at_the_documents_own_canvas_size(job_dir):
    write_plate(job_dir, "plate.png")
    out = R.render(doc_with(art()), job_dir)
    assert out.mode == "RGBA"
    assert out.size == (200, 320)


def test_width_scales_the_whole_render_and_keeps_the_aspect_ratio(job_dir):
    write_plate(job_dir, "plate.png")
    out = R.render(doc_with(art()), job_dir, width=100)
    assert out.size == (100, 160)


def test_width_scales_geometry_rather_than_resampling_the_result(job_dir):
    """Every number in the document is a fraction, so half the width is the
    same picture at half the size — a box that covered the middle fifth still
    covers the middle fifth."""
    small = R.render(doc_with(shape(frame=Frame(x=0.5, y=0.5, w=0.2, h=0.1))),
                     job_dir, width=100)
    big = R.render(doc_with(shape(frame=Frame(x=0.5, y=0.5, w=0.2, h=0.1))),
                   job_dir)
    sx0, sy0, sx1, sy1 = ink_bbox(small)
    bx0, by0, bx1, by1 = ink_bbox(big)
    assert abs((bx0 / 2) - sx0) <= 1 and abs((by0 / 2) - sy0) <= 1
    assert abs((bx1 / 2) - sx1) <= 1 and abs((by1 / 2) - sy1) <= 1


def test_a_zero_width_render_is_refused_with_a_sentence(job_dir):
    with pytest.raises(R.RenderError, match="at least one pixel"):
        R.render(doc_with(shape()), job_dir, width=0)


# -- the stack ----------------------------------------------------------------

def test_the_top_layer_wins_where_two_layers_overlap(job_dir):
    """`layers` is bottom-to-top, the order compose() walks a CoverSpec's own
    layer list — so the last one drawn is the one you see."""
    lower = shape(layer_id="ly_low", fill="#ff0000",
                  frame=Frame(x=0.5, y=0.5, w=0.6, h=0.6))
    upper = shape(layer_id="ly_up", fill="#0000ff",
                  frame=Frame(x=0.5, y=0.5, w=0.3, h=0.3))
    out = R.render(doc_with(lower, upper), job_dir)
    assert out.getpixel((100, 160)) == BLUE       # both boxes: top wins
    assert out.getpixel((100, 80)) == RED         # only the lower box


def test_a_hidden_layer_draws_nothing(job_dir):
    out = R.render(doc_with(shape(visible=False)), job_dir)
    assert ink_bbox(out) is None


def test_locking_a_layer_does_not_hide_it(job_dir):
    """`locked` is about what the transform tools may touch, never about what
    draws (model.LayerBase)."""
    out = R.render(doc_with(shape(locked=True)), job_dir)
    assert ink_bbox(out) is not None


def test_nothing_is_painted_behind_the_bottom_layer(job_dir):
    """Deliberately no `paper` rect: engine.js's export draws one, this
    returns straight RGBA so a measurement pass keeps the alpha."""
    out = R.render(doc_with(shape()), job_dir)
    assert alpha_at(out, 2, 2) == 0


# -- art fits -----------------------------------------------------------------

def test_contain_letterboxes_the_plate_inside_its_box(job_dir):
    source = write_quadrant_plate(job_dir, "quad.png")
    out = R.render(doc_with(art(source=source, fit="contain")),
                   job_dir, width=200)
    assert alpha_at(out, 100, 4) == 0             # letterbox above
    assert alpha_at(out, 100, 316) == 0           # and below
    assert alpha_at(out, 2, 160) == 255           # full width, edge to edge


def test_cover_fills_the_box_and_crops_the_overhang(job_dir):
    source = write_quadrant_plate(job_dir, "quad.png")
    out = R.render(doc_with(art(source=source, fit="cover")), job_dir)
    for probe in ((1, 1), (198, 1), (1, 318), (198, 318), (100, 160)):
        assert alpha_at(out, *probe) == 255
    # The plate's left-tenth marker band is outside the crop.
    assert out.getpixel((3, 160))[:3] != BLACK[:3]


def test_stretch_fills_the_box_and_keeps_what_cover_would_crop(job_dir):
    source = write_quadrant_plate(job_dir, "quad.png")
    out = R.render(doc_with(art(source=source, fit="stretch")), job_dir)
    assert alpha_at(out, 1, 1) == 255
    assert out.getpixel((3, 160))[:3] == BLACK[:3]


def test_the_plate_is_centred_in_its_box(job_dir):
    source = write_plate(job_dir, "plate.png", size=(40, 40))
    layer = art(source=source, fit="contain",
                frame=Frame(x=0.25, y=0.5, w=0.4, h=0.4))
    out = R.render(doc_with(layer), job_dir)
    x0, y0, x1, y1 = ink_bbox(out)
    assert abs(((x0 + x1) / 2) - 0.25 * 200) <= 1
    assert abs(((y0 + y1) / 2) - 0.5 * 320) <= 1


def test_a_missing_plate_names_the_layer_and_the_path_it_looked_for(job_dir):
    with pytest.raises(R.RenderError) as excinfo:
        R.render(doc_with(art(source="assets/gone.png")), job_dir)
    message = str(excinfo.value)
    assert "ly_art" in message
    assert "assets/gone.png" in message
    assert str(job_dir) in message


# -- text ---------------------------------------------------------------------

def test_text_ink_lands_inside_its_own_frame(job_dir):
    layer = text(text="HELLO", size=0.06,
                 frame=Frame(x=0.5, y=0.5, w=0.8, h=0.3))
    out = R.render(doc_with(layer), job_dir)
    x0, y0, x1, y1 = ink_bbox(out)
    margin = 0.06 * 320                    # one em of generosity, no more
    assert x0 >= 0.1 * 200 - margin and x1 <= 0.9 * 200 + margin
    assert y0 >= 0.35 * 320 - margin and y1 <= 0.65 * 320 + margin


def test_align_moves_the_ink_across_the_frame(job_dir):
    def centre(align):
        out = R.render(doc_with(text(align=align)), job_dir)
        x0, _, x1, _ = ink_bbox(out)
        return (x0 + x1) / 2

    assert centre("left") < centre("center") < centre("right")
    assert abs(centre("center") - 100) <= 2


def test_size_is_a_fraction_of_canvas_height(job_dir):
    small = ink_bbox(R.render(doc_with(text(size=0.05)), job_dir))
    large = ink_bbox(R.render(doc_with(text(size=0.10)), job_dir))
    small_h = small[3] - small[1]
    large_h = large[3] - large[1]
    assert large_h > small_h
    assert 1.7 < large_h / small_h < 2.3


def test_line_breaks_are_the_only_breaks_and_they_stack(job_dir):
    one = ink_bbox(R.render(doc_with(text(text="HELLO")), job_dir))
    two = ink_bbox(R.render(doc_with(text(text="HELLO\nWORLD")), job_dir))
    assert (two[3] - two[1]) > (one[3] - one[1])


def test_tracking_widens_a_line(job_dir):
    tight = ink_bbox(R.render(doc_with(text(tracking=0.0)), job_dir))
    loose = ink_bbox(R.render(doc_with(text(tracking=0.15)), job_dir))
    assert (loose[2] - loose[0]) > (tight[2] - tight[0])


def test_a_text_layer_with_no_text_draws_nothing(job_dir):
    assert ink_bbox(R.render(doc_with(text(text="")), job_dir)) is None


def test_an_unregistered_family_is_refused_by_name():
    with pytest.raises(R.RenderError, match="not on the shelf"):
        R._face("Nonesuch Grotesk", "regular", 24.0)


def test_a_style_the_face_does_not_ship_falls_back_to_regular():
    """font_path refuses a companion the family has no file for; a browser
    would synthesize a faux cut, so the renderer draws the regular one rather
    than refusing the layer."""
    assert R._face("Bebas Neue", "italic", 20.0).path == str(
        R._face("Bebas Neue", "regular", 20.0).path)


# -- warps --------------------------------------------------------------------

def _warp_bbox(job_dir, kind, amount):
    layer = text(text="WARPED", size=0.07, warp=Warp(kind=kind, amount=amount))
    return ink_bbox(R.render(doc_with(layer), job_dir))


def test_arc_bows_the_line_taller_than_flat(job_dir):
    flat = _warp_bbox(job_dir, "none", 0.0)
    arced = _warp_bbox(job_dir, "arc", 0.8)
    assert (arced[3] - arced[1]) > (flat[3] - flat[1]) * 1.5


def test_arch_bows_like_arc_but_keeps_the_glyphs_upright(job_dir):
    arced = _warp_bbox(job_dir, "arc", 0.8)
    arched = _warp_bbox(job_dir, "arch", 0.8)
    flat = _warp_bbox(job_dir, "none", 0.0)
    assert (arched[3] - arched[1]) > (flat[3] - flat[1]) * 1.5
    # Upright glyphs on the same circular baseline cannot reach as far as
    # tangent-rotated ones, which lean out at the ends of the sweep.
    assert (arched[3] - arched[1]) < (arced[3] - arced[1])


def test_the_arc_sign_bows_the_line_the_other_way(job_dir):
    layer = text(text="WARPED", size=0.07, warp=Warp(kind="arc", amount=0.8))
    down = ink_bbox(R.render(doc_with(layer), job_dir))
    layer.warp = Warp(kind="arc", amount=-0.8)
    up = ink_bbox(R.render(doc_with(layer), job_dir))
    assert ((down[1] + down[3]) / 2) > ((up[1] + up[3]) / 2)


def test_flag_waves_the_line_taller_without_bowing_it(job_dir):
    flat = _warp_bbox(job_dir, "none", 0.0)
    flagged = _warp_bbox(job_dir, "flag", 0.9)
    assert (flagged[3] - flagged[1]) > (flat[3] - flat[1])
    # One sine period is symmetric about the baseline, so the line's centre
    # stays where a flat line's was.
    assert abs(((flagged[1] + flagged[3]) / 2)
               - ((flat[1] + flat[3]) / 2)) <= 4


def test_bulge_scales_the_advances_and_widens_the_line(job_dir):
    flat = _warp_bbox(job_dir, "none", 0.0)
    bulged = _warp_bbox(job_dir, "bulge", 0.9)
    assert (bulged[2] - bulged[0]) > (flat[2] - flat[0])
    assert (bulged[3] - bulged[1]) > (flat[3] - flat[1])


def test_a_zero_amount_warp_is_flat_whatever_its_kind(job_dir):
    flat = _warp_bbox(job_dir, "none", 0.0)
    nominal = _warp_bbox(job_dir, "arc", 0.0)
    assert nominal == flat


# -- scrim --------------------------------------------------------------------

def test_a_scrim_ramps_its_alpha_across_the_box(job_dir):
    layer = ScrimLayer(id="ly_scrim", frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                       color="#000000",
                       gradient=Gradient(angle=90, stops=[Stop(at=0, alpha=0),
                                                          Stop(at=1, alpha=1)]))
    out = R.render(doc_with(layer), job_dir)
    assert alpha_at(out, 100, 2) <= 4              # angle 90 ramps y-down
    assert alpha_at(out, 100, 317) >= 250
    assert 100 < alpha_at(out, 100, 160) < 160     # linear through the middle


def test_scrim_angle_zero_ramps_left_to_right(job_dir):
    layer = ScrimLayer(id="ly_scrim", frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                       color="#000000",
                       gradient=Gradient(angle=0, stops=[Stop(at=0, alpha=0),
                                                         Stop(at=1, alpha=1)]))
    out = R.render(doc_with(layer), job_dir)
    assert alpha_at(out, 2, 160) <= 4
    assert alpha_at(out, 197, 160) >= 250


def test_a_scrim_darkens_the_end_its_stops_name(job_dir):
    write_plate(job_dir, "plate.png", size=(20, 20), color=WHITE)
    scrim = ScrimLayer(id="ly_scrim", frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                       color="#000000",
                       gradient=Gradient(angle=90, stops=[Stop(at=0, alpha=0),
                                                          Stop(at=1, alpha=1)]))
    out = R.render(doc_with(art(), scrim), job_dir)
    assert out.getpixel((100, 2))[0] > 250         # untouched white
    assert out.getpixel((100, 317))[0] < 6         # fully scrimmed


# -- ornament frames and shapes ----------------------------------------------

def test_an_ornament_frame_is_a_rule_around_a_hole(job_dir):
    layer = FrameLayer(id="ly_frame", frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                       preset="single_rule", stroke="#ffffff", stroke_w=0.01)
    out = R.render(doc_with(layer), job_dir)
    assert alpha_at(out, 100, 160) == 0            # the middle is a hole
    x0, y0, x1, y1 = ink_bbox(out)
    inset = 0.02 * min(200, 320)                   # the model's default inset
    assert abs(x0 - (inset - 0.01 * 200 / 2)) <= 2
    assert abs(y1 - (320 - inset + 0.01 * 200 / 2)) <= 2


def test_corner_serifs_draw_four_corners_and_no_sides(job_dir):
    layer = FrameLayer(id="ly_frame", frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                       preset="corner_serifs", stroke="#ffffff",
                       stroke_w=0.01, inset=0.05)
    out = R.render(doc_with(layer), job_dir)
    inset = int(0.05 * 200)                        # inset is of the SHORT side
    arm = min(200 - 2 * inset, 320 - 2 * inset) * R.CORNER_SERIF_ARM
    assert alpha_at(out, inset, inset + 4) == 255       # down the left arm
    assert alpha_at(out, inset + 4, inset) == 255       # along the top arm
    assert alpha_at(out, inset, int(inset + arm) + 6) == 0   # the arm ends
    assert alpha_at(out, 100, inset) == 0              # no top rule mid-span


def test_a_shape_fills_its_box(job_dir):
    out = R.render(doc_with(shape(frame=Frame(x=0.5, y=0.5, w=0.5, h=0.25))),
                   job_dir)
    x0, y0, x1, y1 = ink_bbox(out)
    assert abs((x1 - x0) - 0.5 * 200) <= 2
    assert abs((y1 - y0) - 0.25 * 320) <= 2


def test_an_ellipse_leaves_its_corners_empty(job_dir):
    layer = shape(shape="ellipse", frame=Frame(x=0.5, y=0.5, w=0.5, h=0.5))
    out = R.render(doc_with(layer), job_dir)
    assert alpha_at(out, 100, 160) == 255
    assert alpha_at(out, int(0.25 * 200) + 3, int(160 - 0.25 * 320) + 3) == 0


def test_a_corner_radius_rounds_the_box(job_dir):
    square = shape(frame=Frame(x=0.5, y=0.5, w=0.5, h=0.5), radius=0.0)
    rounded = shape(frame=Frame(x=0.5, y=0.5, w=0.5, h=0.5), radius=0.5)
    corner = (int(0.25 * 200) + 2, int(160 - 0.25 * 320) + 2)
    assert alpha_at(R.render(doc_with(square), job_dir), *corner) == 255
    assert alpha_at(R.render(doc_with(rounded), job_dir), *corner) == 0


# -- transforms ---------------------------------------------------------------

def test_rotation_turns_the_box_about_its_centre(job_dir):
    flat = shape(frame=Frame(x=0.5, y=0.5, w=0.5, h=0.1))
    spun = shape(frame=Frame(x=0.5, y=0.5, w=0.5, h=0.1, rotation=90))
    wide = ink_bbox(R.render(doc_with(flat), job_dir))
    tall = ink_bbox(R.render(doc_with(spun), job_dir))
    assert abs((tall[3] - tall[1]) - (wide[2] - wide[0])) <= 3
    assert abs((tall[2] - tall[0]) - (wide[3] - wide[1])) <= 3
    # about the centre, not a corner
    assert abs(((tall[0] + tall[2]) / 2) - 100) <= 2
    assert abs(((tall[1] + tall[3]) / 2) - 160) <= 2


def test_flip_h_mirrors_the_layer_about_its_own_box_centre(job_dir):
    plate = Image.new("RGBA", (40, 20), RED)
    plate.paste(Image.new("RGBA", (20, 20), BLUE), (0, 0))
    plate.save(job_dir / "assets/half.png")
    frame = Frame(x=0.5, y=0.5, w=1.0, h=1.0)
    upright = R.render(
        doc_with(art(source="assets/half.png", fit="stretch", frame=frame)),
        job_dir)
    frame = Frame(x=0.5, y=0.5, w=1.0, h=1.0, flip_h=True)
    mirrored = R.render(
        doc_with(art(source="assets/half.png", fit="stretch", frame=frame)),
        job_dir)
    assert upright.getpixel((40, 160))[:3] == BLUE[:3]
    assert mirrored.getpixel((40, 160))[:3] == RED[:3]
    assert mirrored.getpixel((160, 160))[:3] == BLUE[:3]


def test_flip_v_mirrors_the_other_axis(job_dir):
    plate = Image.new("RGBA", (20, 40), RED)
    plate.paste(Image.new("RGBA", (20, 20), BLUE), (0, 0))
    plate.save(job_dir / "assets/half.png")
    frame = Frame(x=0.5, y=0.5, w=1.0, h=1.0, flip_v=True)
    out = R.render(
        doc_with(art(source="assets/half.png", fit="stretch", frame=frame)),
        job_dir)
    assert out.getpixel((100, 300))[:3] == BLUE[:3]


def test_opacity_scales_the_layers_alpha(job_dir):
    out = R.render(doc_with(shape(opacity=0.5)), job_dir)
    assert 120 <= alpha_at(out, 100, 160) <= 136


def test_a_fully_transparent_layer_draws_nothing_visible(job_dir):
    out = R.render(doc_with(shape(opacity=0.0)), job_dir)
    assert ink_bbox(out) is None


# -- effects ------------------------------------------------------------------

def test_a_drop_shadow_paints_outside_the_layers_own_box(job_dir):
    frame = Frame(x=0.5, y=0.5, w=0.2, h=0.1)
    bare = shape(frame=frame)
    shadowed = shape(frame=frame, effects=[Effect(
        type="drop_shadow",
        params={"dx": 0.06, "dy": 0.0, "blur": 0.0, "alpha": 1.0})])
    right_of_box = (int(100 + 0.1 * 200 + 6), 160)
    assert alpha_at(R.render(doc_with(bare), job_dir), *right_of_box) == 0
    assert alpha_at(R.render(doc_with(shadowed), job_dir),
                    *right_of_box) == 255


def test_a_shadows_offset_is_a_fraction_of_canvas_width(job_dir):
    """setShadow multiplies dx/dy/blur by W(), never by the height — the one
    place the engine measures a vertical distance against the width."""
    frame = Frame(x=0.5, y=0.5, w=0.2, h=0.1)
    layer = shape(frame=frame, fill="#ff0000", effects=[Effect(
        type="drop_shadow",
        params={"dx": 0.0, "dy": 0.1, "blur": 0.0, "alpha": 1.0,
                "color": "#0000ff"})])
    out = R.render(doc_with(layer), job_dir)
    below = int(160 + 0.05 * 320 + 0.1 * 200 - 4)
    assert out.getpixel((100, below))[:3] == BLUE[:3]


def test_blur_spreads_a_shadow_past_its_hard_edge(job_dir):
    frame = Frame(x=0.5, y=0.5, w=0.2, h=0.1)
    params = {"dx": 0.0, "dy": 0.0, "alpha": 1.0}
    sharp = shape(frame=frame, effects=[Effect(type="drop_shadow",
                                               params=dict(params, blur=0.0))])
    soft = shape(frame=frame, effects=[Effect(type="drop_shadow",
                                              params=dict(params, blur=0.06))])
    probe = (int(100 + 0.1 * 200 + 5), 160)
    assert alpha_at(R.render(doc_with(sharp), job_dir), *probe) == 0
    assert 0 < alpha_at(R.render(doc_with(soft), job_dir), *probe) < 255


def test_two_shadows_both_paint(job_dir):
    """Konva carries one shadow per shape, so a stack needs a ghost copy of
    the content per extra shadow — both ends have to land."""
    frame = Frame(x=0.5, y=0.5, w=0.2, h=0.1)
    layer = shape(frame=frame, effects=[
        Effect(type="drop_shadow", params={"dx": -0.06, "dy": 0.0,
                                           "blur": 0.0, "alpha": 1.0}),
        Effect(type="drop_shadow", params={"dx": 0.06, "dy": 0.0,
                                           "blur": 0.0, "alpha": 1.0})])
    out = R.render(doc_with(layer), job_dir)
    assert alpha_at(out, int(100 - 0.1 * 200 - 6), 160) == 255
    assert alpha_at(out, int(100 + 0.1 * 200 + 6), 160) == 255


def test_a_bevel_puts_light_up_left_and_dark_down_right(job_dir):
    write_plate(job_dir, "plate.png", size=(20, 20), color=(128, 128, 128, 255))
    frame = Frame(x=0.5, y=0.5, w=0.4, h=0.4)
    layer = art(frame=frame, fit="stretch",
                effects=[Effect(type="bevel", params={"depth": 1.0})])
    out = R.render(doc_with(layer), job_dir)
    depth = max(0.5, 1.0 * 0.006 * 200)
    left, top = 100 - 0.2 * 200, 160 - 0.2 * 320
    up_left = out.getpixel((int(left - depth / 2), int(top - depth / 2)))
    down_right = out.getpixel((int(100 + 0.2 * 200 + depth / 2),
                               int(160 + 0.2 * 320 + depth / 2)))
    assert up_left[0] > 160          # the white ghost
    assert down_right[0] < 96        # the black one


def test_levels_brightens_a_plate(job_dir):
    write_plate(job_dir, "plate.png", size=(20, 20), color=(100, 100, 100, 255))
    plain = R.render(doc_with(art()), job_dir).getpixel((100, 160))
    lifted = R.render(
        doc_with(art(effects=[Effect(type="levels",
                                     params={"brightness": 0.15,
                                             "contrast": 0.0})])),
        job_dir).getpixel((100, 160))
    assert lifted[0] > plain[0]
    assert abs(lifted[0] - (100 + 0.15 * 255)) <= 2


def test_levels_contrast_widens_about_mid_grey(job_dir):
    write_plate(job_dir, "plate.png", size=(20, 20), color=(80, 80, 80, 255))
    out = R.render(
        doc_with(art(effects=[Effect(type="levels",
                                     params={"brightness": 0.0,
                                             "contrast": 0.5})])),
        job_dir)
    expected = ((80 / 255 - 0.5) * 1.5 + 0.5) * 255
    assert abs(out.getpixel((100, 160))[0] - expected) <= 2


def test_levels_leaves_alpha_alone(job_dir):
    write_plate(job_dir, "plate.png", size=(20, 20), color=(10, 10, 10, 255))
    out = R.render(
        doc_with(art(opacity=0.5,
                     effects=[Effect(type="levels",
                                     params={"brightness": 0.4,
                                             "contrast": 0.0})])),
        job_dir)
    assert 120 <= alpha_at(out, 100, 160) <= 136


def test_an_unknown_effect_type_is_skipped_in_silence(job_dir):
    """The client draws what it knows and ignores the rest (model.Effect is
    the one extra="allow" model for exactly this reason); so does this."""
    plain = R.render(doc_with(shape()), job_dir)
    odd = R.render(
        doc_with(shape(effects=[Effect(type="edge_glow", params={"r": 3})])),
        job_dir)
    assert odd.tobytes() == plain.tobytes()


# -- the corner pin -----------------------------------------------------------

def pinned_plate(job_dir) -> str:
    """A plate whose top-left corner is identifiable, so a pin can be caught
    putting it in the wrong place rather than merely somewhere."""
    img = Image.new("RGBA", (40, 40), RED)
    img.paste(Image.new("RGBA", (10, 10), GREEN), (0, 0))
    img.save(job_dir / "assets/pin.png")
    return "assets/pin.png"


def test_a_pin_maps_the_plate_onto_its_four_canvas_points(job_dir):
    source = pinned_plate(job_dir)
    frame = Frame(x=0.5, y=0.5, w=0.2, h=0.2,
                  corners=[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])
    out = R.render(doc_with(art(source=source, frame=frame)),
                   job_dir, width=200)
    assert alpha_at(out, 100, 160) == 255                  # inside the quad
    assert alpha_at(out, 5, 5) == 0                        # outside it
    # The plate's own top-left corner sits on the first pinned point.
    assert out.getpixel((int(0.1 * 200) + 4, int(0.1 * 320) + 4))[:3] == \
        GREEN[:3]
    assert out.getpixel((int(0.9 * 200) - 6, int(0.9 * 320) - 6))[:3] == \
        RED[:3]


def test_a_pin_overrides_the_box_the_layer_would_otherwise_occupy(job_dir):
    """`fit` and the box have no meaning while pinned — the quad IS the
    destination (buildPinnedArt)."""
    source = pinned_plate(job_dir)
    corners = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    tiny = Frame(x=0.5, y=0.5, w=0.05, h=0.05, corners=corners)
    huge = Frame(x=0.1, y=0.2, w=1.0, h=1.0, corners=corners)
    first = R.render(doc_with(art(source=source, frame=tiny)), job_dir)
    second = R.render(doc_with(art(source=source, frame=huge)), job_dir)
    assert ink_bbox(first) == ink_bbox(second)


def test_a_pinned_corner_follows_a_slanted_quad(job_dir):
    source = pinned_plate(job_dir)
    corners = [[0.30, 0.10], [0.90, 0.25], [0.85, 0.80], [0.15, 0.70]]
    frame = Frame(x=0.5, y=0.5, w=0.5, h=0.5, corners=corners)
    out = R.render(doc_with(art(source=source, frame=frame)), job_dir)
    x0, y0, x1, y1 = ink_bbox(out)
    xs = [c[0] * 200 for c in corners]
    ys = [c[1] * 320 for c in corners]
    assert abs(x0 - min(xs)) <= 2 and abs(x1 - max(xs)) <= 2
    assert abs(y0 - min(ys)) <= 2 and abs(y1 - max(ys)) <= 2
    # and the identifiable corner is at the first pinned point, not elsewhere
    assert out.getpixel((int(0.30 * 200) + 6, int(0.10 * 320) + 8))[:3] == \
        GREEN[:3]


def test_a_pinned_plate_ignores_rotation_and_flip(job_dir):
    """pinLocal walks each absolute corner backwards through the group's own
    transform, so the group re-applying it cancels exactly: a pin states where
    the pixels land, full stop."""
    source = pinned_plate(job_dir)
    corners = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
    plain = Frame(x=0.5, y=0.5, w=0.3, h=0.3, corners=corners)
    turned = Frame(x=0.5, y=0.5, w=0.3, h=0.3, corners=corners,
                   rotation=37, flip_h=True)
    first = R.render(doc_with(art(source=source, frame=plain)), job_dir)
    second = R.render(doc_with(art(source=source, frame=turned)), job_dir)
    assert first.tobytes() == second.tobytes()


def test_a_pin_on_a_non_art_layer_draws_the_undistorted_box(job_dir):
    """engine.js pins art and nothing else, and the doc's own promise is that
    a client which cannot draw the pin is undistorted, never misplaced."""
    corners = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    boxed = shape(frame=Frame(x=0.5, y=0.5, w=0.3, h=0.3))
    pinned = shape(frame=Frame(x=0.5, y=0.5, w=0.3, h=0.3, corners=corners))
    assert ink_bbox(R.render(doc_with(pinned), job_dir)) == \
        ink_bbox(R.render(doc_with(boxed), job_dir))


def test_a_collapsed_pin_is_refused_with_a_sentence(job_dir):
    source = pinned_plate(job_dir)
    frame = Frame(x=0.5, y=0.5, w=0.3, h=0.3,
                  corners=[[0.1, 0.1], [0.5, 0.1], [0.9, 0.1], [0.5, 0.1]])
    with pytest.raises(R.RenderError, match="collinear"):
        R.render(doc_with(art(source=source, frame=frame)), job_dir)


# -- the parity table ---------------------------------------------------------

def test_every_geometric_constant_still_matches_engine_js():
    """The whole point of keeping these in one block: engine.js and this
    module drift loudly. Each literal here is the expression named in the
    module's parity table — change one side and this test says so."""
    assert R.MIN_BOX_FRACTION == 1e-4              # boxOf
    assert R.TEXT_MIN_FONT_PX == 1.0               # buildText
    assert R.TEXT_DEFAULT_SIZE == 0.05
    assert R.TEXT_DEFAULT_LINE_HEIGHT == 1.15
    assert R.WARP_SWEEP_RAD == math.pi * 0.75      # |amount| * 135 degrees
    assert R.WARP_MIN_SWEEP == 1e-3
    assert R.BULGE_GAIN == 0.55
    assert R.FLAG_AMP_EMS == 0.45
    assert R.FRAME_MIN_STROKE_PX == 0.4
    assert R.FRAME_DEFAULT_STROKE_W == 0.002
    assert R.FRAME_MIN_SIDE_PX == 1.0
    assert R.CORNER_SERIF_ARM == 0.16
    assert R.DOUBLE_RULE_GAP_STROKES == 3.0
    assert R.DOUBLE_RULE_GAP_FRACTION == 0.022
    assert R.DOUBLE_RULE_INNER_STROKE == 0.6
    assert R.INSET_PANEL_GAP_STROKES == 2.6
    assert R.INSET_PANEL_INNER_STROKE == 0.5
    assert R.INSET_PANEL_INNER_OPACITY == 0.6
    assert R.BEVEL_DEFAULT_DEPTH == 0.3
    assert R.BEVEL_MIN_PX == 0.5
    assert R.BEVEL_DEPTH_TO_PX == 0.006
    assert R.BEVEL_ALPHA == 0.65
    assert (R.BEVEL_LIGHT, R.BEVEL_DARK) == ("#ffffff", "#000000")
    assert R.SHADOW_DEFAULT_ALPHA == 0.5
    assert R.SHADOW_DEFAULT_COLOR == "#000000"
    assert R.SCRIM_FALLBACK_ANGLE == 90.0
    assert R.PIN_KINDS == ("art",)
    assert R.SQUARE_TO_QUAD_EPS == 1e-9
    assert (R.LEVELS_CONTRAST_MIN, R.LEVELS_CONTRAST_MAX) == (-0.95, 4.0)
    assert R.SHADOW_BLUR_TO_SIGMA == 0.5           # canvas: blur = 2 * sigma


def test_square_to_quad_takes_the_unit_square_to_the_named_corners():
    """The closed form engine.js uses, checked at its four fixed points: a
    projective map is only ever as right as its corners."""
    quad = [(30.0, 10.0), (90.0, 25.0), (85.0, 80.0), (15.0, 70.0)]
    m = R._square_to_quad(quad)
    for (u, v), (x, y) in zip([(0, 0), (1, 0), (1, 1), (0, 1)], quad):
        w = m[6] * u + m[7] * v + 1.0
        assert abs((m[0] * u + m[1] * v + m[2]) / w - x) < 1e-6
        assert abs((m[3] * u + m[4] * v + m[5]) / w - y) < 1e-6


def test_a_parallelogram_takes_the_affine_branch():
    """Both projective terms are zero exactly when the quad is a
    parallelogram — the branch a freshly pinned, still-rectangular layer
    takes."""
    m = R._square_to_quad([(0.0, 0.0), (10.0, 0.0), (10.0, 8.0), (0.0, 8.0)])
    assert (m[6], m[7]) == (0.0, 0.0)


def test_an_inverted_matrix_round_trips():
    m = R._square_to_quad([(30.0, 10.0), (90.0, 25.0), (85.0, 80.0),
                           (15.0, 70.0)])
    inverse = R._invert3(m)
    assert inverse is not None
    for x, y in ((30.0, 10.0), (85.0, 80.0)):
        w = inverse[6] * x + inverse[7] * y + inverse[8]
        u = (inverse[0] * x + inverse[1] * y + inverse[2]) / w
        v = (inverse[3] * x + inverse[4] * y + inverse[5]) / w
        assert -1e-6 < u < 1 + 1e-6 and -1e-6 < v < 1 + 1e-6


def test_a_singular_matrix_inverts_to_nothing():
    assert R._invert3((1.0, 2.0, 3.0, 2.0, 4.0, 6.0, 0.0, 0.0, 1.0)) is None
