"""docproof/canvas/ingest.py: a finished cover job, opened as layers.

Every job dir here is fabricated the way the pipeline writes one — a real
JobState dumped to job.json, real PNGs under assets/ — so these tests fail if
the pipeline's on-disk layout moves, which is the point: ingest is the one
module that has to know it. Specs are built from the real shipped archetypes
(the same call test_cover_model.py and test_cover_compose.py make), because
the z-order and unit conversions under test are only interesting against
compositions somebody actually ships.

Canvas is 400x640 throughout except where the default is under test: all
geometry is fractional, and a small canvas runs the same typeset fit search
in a fraction of the time.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from docproof.canvas.ingest import CanvasIngestError, ingest
from docproof.canvas.model import load_doc, save_doc
from docproof.cover import typeset
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.compose import EBOOK_H, EBOOK_W
from docproof.cover.model import (Brief, ConceptState, CoverSpec, Direction,
                                  JobState, Palette, RenderReport, build_spec)

CANVAS = (400, 640)
PLATE = (512, 512)


# -- fixtures -----------------------------------------------------------------

def _palette(**overrides) -> Palette:
    data = dict(background="#101820", primary="#f5f1e8", accent="#c9a227",
                text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
                genre="literary")
    data.update(overrides)
    return Brief(**data)


def _spec(archetype_name: str = "cutout_sandwich", **direction_overrides
          ) -> CoverSpec:
    archetype = ARCHETYPES[archetype_name]
    data = dict(concept_name="Ash and Brass",
                rationale="A brooding industrial-fantasy palette.",
                archetype=archetype_name, palette=_palette(),
                title_font="Playfair Display", author_font="Spectral",
                art_prompts={slot.id: "a smoky brass foundry at dusk"
                             for slot in archetype.art if slot.generatable},
                texture=False)
    data.update(direction_overrides)
    return build_spec(Direction(**data), _brief(), archetype)


def _paint_assets(job_dir: Path, spec: CoverSpec, index: int = 0,
                  size: tuple[int, int] = PLATE) -> None:
    """Give every generatable slot a real plate under assets/, named the way
    docproof.cover.pipeline._generate_art_slot names one."""
    (job_dir / "assets").mkdir(parents=True, exist_ok=True)
    for slot in spec.art:
        if not slot.prompt:
            continue
        rel = f"assets/c{index}_{slot.id}.png"
        Image.new("RGBA", size, (10, 20, 30, 255)).save(job_dir / rel)
        slot.asset = rel


def _job_dir(tmp_path: Path, *specs: CoverSpec,
             reports: list[RenderReport | None] | None = None,
             paint: bool = True, job_id: str = "20260831T120000Z-a1b2c3"
             ) -> Path:
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    reports = reports or [None] * len(specs)
    concepts = []
    for i, spec in enumerate(specs):
        if paint:
            _paint_assets(job_dir, spec, index=i)
        concepts.append(ConceptState(spec=spec, status="ready",
                                     report=reports[i]))
    job = JobState(job_id=job_id, brief=_brief(), status="ready",
                   created="2026-08-31T12:00:00+00:00", concepts=concepts)
    (job_dir / "job.json").write_text(job.model_dump_json(indent=2),
                                      encoding="utf-8")
    return job_dir


def _report(**overrides) -> RenderReport:
    data = dict(contrast={}, scrim_final={}, fitted_sizes={}, warnings=[])
    data.update(overrides)
    return RenderReport(**data)


# -- the job on disk ----------------------------------------------------------

def test_a_job_without_a_manifest_names_the_file(tmp_path):
    (tmp_path / "empty-job").mkdir()
    with pytest.raises(CanvasIngestError, match="job.json"):
        ingest(tmp_path / "empty-job")


def test_unparseable_json_names_the_file(tmp_path):
    job_dir = tmp_path / "broken"
    job_dir.mkdir()
    (job_dir / "job.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CanvasIngestError, match="job.json could not be read"):
        ingest(job_dir)


def test_a_job_with_no_concepts_says_there_is_no_cover(tmp_path):
    job_dir = _job_dir(tmp_path)
    with pytest.raises(CanvasIngestError, match="no concepts"):
        ingest(job_dir)


def test_a_concept_that_does_not_exist_says_how_many_there_are(tmp_path):
    job_dir = _job_dir(tmp_path, _spec())
    with pytest.raises(CanvasIngestError, match="1 concept"):
        ingest(job_dir, concept=3, canvas=CANVAS)


def test_a_concept_that_never_got_a_spec_is_named(tmp_path):
    job_dir = _job_dir(tmp_path, _spec())
    raw = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    del raw["concepts"][0]["spec"]
    (job_dir / "job.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CanvasIngestError, match="carries no spec"):
        ingest(job_dir, canvas=CANVAS)


def test_a_spec_that_does_not_validate_is_reported_not_guessed_at(tmp_path):
    job_dir = _job_dir(tmp_path, _spec())
    raw = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    raw["concepts"][0]["spec"]["palette"]["text"] = "chartreuse"
    (job_dir / "job.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CanvasIngestError, match="does not validate"):
        ingest(job_dir, canvas=CANVAS)


def test_concept_picks_which_cover_opens(tmp_path):
    first, second = _spec("cutout_sandwich"), _spec("full_bleed_art")
    job_dir = _job_dir(tmp_path, first, second)
    assert ingest(job_dir, canvas=CANVAS).source_spec["archetype"] == \
        "cutout_sandwich"
    assert ingest(job_dir, concept=1, canvas=CANVAS).source_spec["archetype"] \
        == "full_bleed_art"


def test_a_missing_plate_names_the_file_it_looked_for(tmp_path):
    spec = _spec("cutout_sandwich")
    job_dir = _job_dir(tmp_path, spec)
    (job_dir / spec.art[0].asset).unlink()
    with pytest.raises(CanvasIngestError) as excinfo:
        ingest(job_dir, canvas=CANVAS)
    assert "c0_background.png" in str(excinfo.value)


def test_a_plate_that_is_not_an_image_names_the_file(tmp_path):
    spec = _spec("cutout_sandwich")
    job_dir = _job_dir(tmp_path, spec)
    (job_dir / spec.art[0].asset).write_text("not a png", encoding="utf-8")
    with pytest.raises(CanvasIngestError, match="could not be read as an image"):
        ingest(job_dir, canvas=CANVAS)


# -- the document ------------------------------------------------------------

def test_the_document_carries_the_jobs_own_id_and_canvas(tmp_path):
    job_dir = _job_dir(tmp_path, _spec(), job_id="20260831T120000Z-ffff99")
    doc = ingest(job_dir, canvas=CANVAS)
    assert doc.job_id == "20260831T120000Z-ffff99"
    assert (doc.canvas.w, doc.canvas.h) == CANVAS


def test_the_default_canvas_is_the_composers_ebook_target(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("big_type")))
    assert (doc.canvas.w, doc.canvas.h) == (EBOOK_W, EBOOK_H)


def test_a_fresh_canvas_session_starts_at_zero_spend(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec()), canvas=CANVAS)
    assert doc.cost_usd == 0.0
    assert doc.history == []


def test_the_whole_spec_is_kept_verbatim_for_provenance(tmp_path):
    spec = _spec("full_bleed_art")
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    assert doc.source_spec == spec.model_dump(mode="json")


def test_an_ingested_document_survives_a_save_and_load(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("full_bleed_art")), canvas=CANVAS)
    path = tmp_path / "canvas.json"
    save_doc(doc, path)
    assert load_doc(path).model_dump() == doc.model_dump()


# -- z-order ------------------------------------------------------------------

def test_layers_come_out_in_the_order_compose_draws_them(tmp_path):
    # cutout_sandwich: background, subtitle, title, focal, author -- the
    # cutout figure sits BETWEEN the title and the author line (§5.2.3), and
    # the empty subtitle draws nothing.
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    assert [(l.kind, l.name) for l in doc.layers] == [
        ("art", "background"), ("text", "title"), ("art", "focal"),
        ("text", "author")]


def test_adjust_layers_are_dropped_and_the_rest_keeps_its_order(tmp_path):
    # full_bleed_art finishes with three adjust layers and a procedural
    # grain plate; neither has anything this vocabulary can carry.
    doc = ingest(_job_dir(tmp_path, _spec("full_bleed_art")), canvas=CANVAS)
    assert [(l.kind, l.name) for l in doc.layers] == [
        ("art", "background"),
        ("scrim", "scrim 0 (gradient_down)"),
        ("scrim", "scrim 1 (gradient_down)"),
        ("text", "title"), ("text", "author")]


def test_an_empty_text_slot_becomes_no_layer(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    assert "subtitle" not in [l.name for l in doc.layers]


def test_a_procedural_field_with_no_plate_opens_on_the_right_ground(tmp_path):
    # big_type is fully procedural: its background synthesizes a gradient in
    # the palette's background role, and its rule_frame/grain slots are
    # ornament and texture, which a flat rectangle would misrepresent.
    doc = ingest(_job_dir(tmp_path, _spec("big_type")), canvas=CANVAS)
    ground = doc.layers[0]
    assert (ground.kind, ground.name) == ("scrim", "background")
    assert ground.color == "#101820"
    assert (ground.frame.w, ground.frame.h) == (1.0, 1.0)
    assert "rule_frame" not in [l.name for l in doc.layers]
    assert "fx_grain" not in [l.name for l in doc.layers]


# -- art ----------------------------------------------------------------------

def test_a_cover_fit_plate_lands_where_compose_would_have_put_it(tmp_path):
    # 512x512 into 400x640 fills by 640/512 = 1.25, so the plate is 640x640:
    # exactly the canvas height, 1.6x its width, centered.
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    frame = doc.layers[0].frame
    assert (frame.x, frame.y) == (0.5, 0.5)
    assert (frame.w, frame.h) == pytest.approx((1.6, 1.0))


def test_a_contain_fit_cutout_keeps_its_anchor_and_scale(tmp_path):
    # cutout_sandwich's focal sits at anchor (0.66, 1.0), scale 0.62: a
    # 248x248 figure standing on the bottom edge, right of center.
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    focal = next(l for l in doc.layers if l.name == "focal")
    assert (focal.frame.w, focal.frame.h) == pytest.approx((0.62, 0.3875))
    assert (focal.frame.x, focal.frame.y) == pytest.approx((0.56, 0.80625))


def test_the_cutout_flag_rides_along_because_regeneration_needs_it(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    by_name = {l.name: l for l in doc.layers if l.kind == "art"}
    assert by_name["focal"].transparent is True
    assert by_name["background"].transparent is False


def test_the_plate_source_is_the_job_relative_path_the_pipeline_wrote(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    assert doc.layers[0].source == "assets/c0_background.png"


def test_an_art_layer_carries_the_assembled_prompt_not_the_raw_one(tmp_path):
    # A re-roll has to ask for the same thing the pipeline asked for --
    # composition note, cutout directive and negative suffix included.
    from docproof.cover.imaging import CUTOUT_SUFFIX, NEGATIVE_SUFFIX
    archetype = ARCHETYPES["cutout_sandwich"]
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    focal = next(l for l in doc.layers if l.name == "focal")
    assert focal.prompt.startswith("a smoky brass foundry at dusk")
    assert archetype.composition_note in focal.prompt
    assert CUTOUT_SUFFIX in focal.prompt
    assert focal.prompt.endswith(NEGATIVE_SUFFIX)


def test_a_non_cutout_slot_gets_no_cutout_directive(tmp_path):
    from docproof.cover.imaging import CUTOUT_SUFFIX
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    assert CUTOUT_SUFFIX not in doc.layers[0].prompt


def test_an_archetype_that_left_the_shelf_still_opens(tmp_path):
    spec = _spec("cutout_sandwich")
    spec.archetype = "an_archetype_since_retired"
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    assert doc.layers[0].prompt == "a smoky brass foundry at dusk"


def test_the_slots_own_opacity_and_fit_ride_across(tmp_path):
    spec = _spec("cutout_sandwich")
    spec.art[0].opacity = 0.65
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    assert doc.layers[0].opacity == 0.65
    assert doc.layers[0].fit == "cover"
    assert next(l for l in doc.layers if l.name == "focal").fit == "contain"


# -- text ---------------------------------------------------------------------

def test_a_title_arrives_already_broken_because_the_canvas_never_wraps(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert "\n" in title.text
    assert title.text.replace("\n", " ") == "THE LIGHTHOUSE AT GULL POINT"


def test_the_case_the_composer_applied_is_baked_in(tmp_path):
    # cutout_sandwich's title slot is case="upper"; the canvas has no case
    # field, so the ingest resolves it once.
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title.text.isupper()


def test_the_size_is_the_fitted_size_not_the_slots_ceiling(tmp_path):
    spec = _spec("cutout_sandwich")
    title_slot = next(t for t in spec.text if t.id == "title")
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    expected = typeset.fit_text(title_slot, CANVAS).size_frac
    assert title.size == pytest.approx(expected)
    assert title.size < title_slot.size_max


def test_tracking_converts_from_em_per_thousand_to_ems(tmp_path):
    spec = _spec("cutout_sandwich")
    title_slot = next(t for t in spec.text if t.id == "title")
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title_slot.tracking > 1.0            # the spec's own unit
    assert title.tracking == pytest.approx(title_slot.tracking / 1000.0)


def test_the_line_height_matches_the_composers(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title.line_height == typeset.LINE_HEIGHT


def test_the_color_comes_from_the_palette_role(tmp_path):
    spec = _spec("cutout_sandwich", palette=_palette(text="#ffeecc"))
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title.color == "#ffeecc"


def test_a_knockout_slot_takes_the_primary_role_the_composer_tests(tmp_path):
    spec = _spec("cutout_sandwich", palette=_palette(primary="#00ff00"))
    next(t for t in spec.text if t.id == "title").mode = "knockout"
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title.color == "#00ff00"


def test_the_family_is_the_one_the_direction_picked(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    by_name = {l.name: l for l in doc.layers if l.kind == "text"}
    assert by_name["title"].family == "Playfair Display"
    assert by_name["author"].family == "Spectral"


@pytest.mark.parametrize("valign,expected_y", [
    ("top", "top"), ("middle", "middle"), ("bottom", "bottom")])
def test_valign_is_resolved_into_where_the_block_actually_sits(tmp_path, valign,
                                                               expected_y):
    spec = _spec("cutout_sandwich")
    title_slot = next(t for t in spec.text if t.id == "title")
    title_slot.valign = valign
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    zone = title_slot.zone
    if expected_y == "top":
        assert title.frame.y == pytest.approx(zone.y + title.frame.h / 2)
    elif expected_y == "bottom":
        assert title.frame.y == pytest.approx(
            zone.y + zone.h - title.frame.h / 2)
    else:
        assert title.frame.y == pytest.approx(zone.y + zone.h / 2)


def test_the_text_box_is_the_block_the_type_actually_fills(tmp_path):
    spec = _spec("cutout_sandwich")
    title_slot = next(t for t in spec.text if t.id == "title")
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    lines = title.text.split("\n")
    assert title.frame.h == pytest.approx(
        len(lines) * title.size * typeset.LINE_HEIGHT)
    assert title.frame.w == title_slot.zone.w


def test_a_signature_tilt_becomes_a_frame_rotation(tmp_path):
    spec = _spec("cutout_sandwich")
    next(t for t in spec.text if t.id == "title").rotate = -6.0
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title.frame.rotation == -6.0


def test_a_spec_arc_becomes_the_warp_dial(tmp_path):
    spec = _spec("cutout_sandwich")
    next(t for t in spec.text if t.id == "title").arc = 0.175   # half of 0.35
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title.warp.kind == "arc"
    assert title.warp.amount == pytest.approx(0.5)


def test_a_straight_slot_has_no_warp(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("cutout_sandwich")), canvas=CANVAS)
    title = next(l for l in doc.layers if l.name == "title")
    assert title.warp.kind == "none"


# -- scrims -------------------------------------------------------------------

def test_a_protecting_scrim_takes_the_composers_own_four_percent_padding(tmp_path):
    spec = _spec("full_bleed_art")
    doc = ingest(_job_dir(tmp_path, spec), canvas=CANVAS)
    title_zone = next(t for t in spec.text if t.id == "title").zone
    scrim = next(l for l in doc.layers if l.name.startswith("scrim 0"))
    # gradient_down extends the box to the canvas bottom (§7.3), so only the
    # horizontal extent is the padded zone's.
    left = title_zone.x - 0.04
    right = title_zone.x + title_zone.w + 0.04
    assert scrim.frame.w == pytest.approx(right - left)
    assert scrim.frame.x == pytest.approx((left + right) / 2)


def test_a_gradient_down_scrim_ramps_then_stays_solid_to_the_edge(tmp_path):
    doc = ingest(_job_dir(tmp_path, _spec("full_bleed_art")), canvas=CANVAS)
    scrim = next(l for l in doc.layers if l.name.startswith("scrim 0"))
    stops = scrim.gradient.stops
    assert scrim.gradient.angle == 90.0
    assert stops[0].alpha == 0.0
    assert stops[-1].alpha == pytest.approx(0.25)      # the spec's strength
    assert stops[-1].at == 1.0
    assert scrim.frame.y + scrim.frame.h / 2 == pytest.approx(1.0)


def test_the_scrim_strength_is_the_one_the_render_escalated_to(tmp_path):
    # The legibility autopilot escalates at render time and records where it
    # landed; a spec-strength scrim would open weaker than the approved cover.
    spec = _spec("full_bleed_art")
    job_dir = _job_dir(tmp_path, spec,
                       reports=[_report(scrim_final={0: 0.7, 1: 0.15})])
    doc = ingest(job_dir, canvas=CANVAS)
    scrim = next(l for l in doc.layers if l.name.startswith("scrim 0"))
    assert scrim.gradient.stops[-1].alpha == pytest.approx(0.7)


def test_a_scrim_the_composer_never_paints_becomes_no_layer(tmp_path):
    # big_type's two panel scrims sit at strength 0 unless the autopilot
    # raises them.
    doc = ingest(_job_dir(tmp_path, _spec("big_type")), canvas=CANVAS)
    assert not [l for l in doc.layers if l.name.startswith("scrim")]


def test_an_escalated_panel_scrim_becomes_a_flat_alpha_rectangle(tmp_path):
    spec = _spec("big_type")
    job_dir = _job_dir(tmp_path, spec, reports=[_report(scrim_final={0: 0.4})])
    doc = ingest(job_dir, canvas=CANVAS)
    scrim = next(l for l in doc.layers if l.name.startswith("scrim 0"))
    assert [s.alpha for s in scrim.gradient.stops] == [0.4, 0.4]
    assert scrim.color == "#000000"                    # the scrim role
