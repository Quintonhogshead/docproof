"""docproof/cover/model.py: spec/brief validation edges and build_spec's
archetype+direction+brief merge.

No network anywhere here — every model in docproof.cover.model is a plain
pydantic type, and build_spec is pure data transformation over the real
shipped archetypes (docproof/cover/archetypes.py), so these tests exercise
the actual config/cover/archetypes/*.yaml files rather than fixtures.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.model import (ArtSlot, Brief, ConceptState, CoverSpec,
                                  Direction, Directions, JobState, LayerRef,
                                  Palette, PaletteRole, RenderReport,
                                  ScrimSpec, Shadow, Stroke, TextSlot, Zone,
                                  build_spec)

# -- fixtures -----------------------------------------------------------------

def _palette(**overrides) -> Palette:
    data = dict(background="#101010", primary="#f5f1e8", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


def _direction(**overrides) -> Direction:
    data = dict(
        concept_name="Ash and Brass",
        rationale="A brooding industrial-fantasy palette with warm brass accents.",
        archetype="full_bleed_art",
        palette=_palette(),
        title_font="Playfair Display",
        author_font="Spectral",
        art_prompts={"background": "A smoky brass foundry at dusk, oil painting."},
        texture=True)
    data.update(overrides)
    return Direction(**data)


def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary")
    data.update(overrides)
    return Brief(**data)


def _text_slot(**overrides) -> TextSlot:
    data = dict(id="title", zone=Zone(x=0.1, y=0.1, w=0.8, h=0.2),
               font_family="Spectral", size_min=0.02, size_max=0.1)
    data.update(overrides)
    return TextSlot(**data)


# -- extra="forbid" everywhere --------------------------------------------

_ALL_MODELS = [Brief, Palette, Zone, Shadow, Stroke, TextSlot, ArtSlot,
              ScrimSpec, LayerRef, CoverSpec, RenderReport, Direction,
              Directions, ConceptState, JobState]


@pytest.mark.parametrize("model_cls", _ALL_MODELS, ids=lambda c: c.__name__)
def test_every_model_forbids_extra_fields_in_config(model_cls):
    assert model_cls.model_config.get("extra") == "forbid"


def test_zone_actually_rejects_an_unknown_field_at_runtime():
    with pytest.raises(ValidationError):
        Zone(x=0.0, y=0.0, w=0.5, h=0.5, opacity=1.0)


def test_jobstate_actually_rejects_an_unknown_field_at_runtime():
    with pytest.raises(ValidationError):
        JobState(job_id="x", brief=_brief(), status="directing",
                 created="2026-08-28T00:00:00Z", bogus=True)


# -- Brief ----------------------------------------------------------------

def test_brief_requires_title():
    with pytest.raises(ValidationError):
        Brief(author="J. R. Vance", genre="literary")


def test_brief_requires_author():
    with pytest.raises(ValidationError):
        Brief(title="The Lighthouse at Gull Point", genre="literary")


def test_brief_requires_genre():
    with pytest.raises(ValidationError):
        Brief(title="The Lighthouse at Gull Point", author="J. R. Vance")


def test_brief_title_rejects_over_200_chars():
    with pytest.raises(ValidationError):
        _brief(title="x" * 201)


def test_brief_pitch_rejects_over_4000_chars():
    with pytest.raises(ValidationError):
        _brief(pitch="x" * 4001)


@pytest.mark.parametrize("concepts", [1, 4, 6])
def test_brief_concepts_accepts_the_documented_range(concepts):
    assert _brief(concepts=concepts).concepts == concepts


@pytest.mark.parametrize("concepts", [0, 7, -1])
def test_brief_concepts_rejects_outside_the_documented_range(concepts):
    with pytest.raises(ValidationError):
        _brief(concepts=concepts)


def test_brief_genre_is_free_text_not_a_closed_choice():
    # genre accepts one of the 10 subject keys OR free text — it must NOT be
    # constrained to a Literal the way font_family/title_font are.
    assert _brief(genre="a genre nobody has heard of").genre == \
        "a genre nobody has heard of"


# -- Palette / hex validation -----------------------------------------------

@pytest.mark.parametrize("bad", ["red", "#fff", "#gggggg", "101010",
                                 "#1010100", "#12345", ""])
def test_palette_rejects_bad_hex(bad):
    with pytest.raises(ValidationError):
        _palette(background=bad)


def test_palette_accepts_good_hex():
    assert _palette(background="#1a2b3c").background == "#1a2b3c"


def test_palette_get_by_role_enum_and_by_string():
    p = _palette(accent="#c9a227")
    assert p.get(PaletteRole.accent) == "#c9a227"
    assert p.get("accent") == "#c9a227"


def test_palette_get_rejects_unknown_role():
    with pytest.raises(ValueError):
        _palette().get("nonexistent")


def test_shadow_rejects_bad_hex_color():
    with pytest.raises(ValidationError):
        Shadow(color="not-a-color")


def test_shadow_defaults():
    s = Shadow()
    assert s.color == "#000000"
    assert 0.0 <= s.alpha <= 1.0


def test_stroke_rejects_bad_hex_color():
    with pytest.raises(ValidationError):
        Stroke(color="#zzzzzz")


# -- Zone: validated inside the canvas ---------------------------------------

def test_zone_rejects_running_past_the_right_edge():
    with pytest.raises(ValidationError):
        Zone(x=0.7, y=0.1, w=0.5, h=0.1)


def test_zone_rejects_running_past_the_bottom_edge():
    with pytest.raises(ValidationError):
        Zone(x=0.1, y=0.7, w=0.1, h=0.5)


def test_zone_accepts_the_full_canvas():
    z = Zone(x=0.0, y=0.0, w=1.0, h=1.0)
    assert (z.x, z.y, z.w, z.h) == (0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize("w,h", [(0.0, 0.5), (0.5, 0.0), (-0.1, 0.5)])
def test_zone_rejects_non_positive_dimensions(w, h):
    with pytest.raises(ValidationError):
        Zone(x=0.0, y=0.0, w=w, h=h)


# -- TextSlot -----------------------------------------------------------------

def test_textslot_rejects_unregistered_font_family():
    with pytest.raises(ValidationError, match="not registered"):
        _text_slot(font_family="Comic Sans")


def test_textslot_accepts_every_registered_font_family():
    from docproof.cover.fonts import FAMILIES
    for name in FAMILIES:
        assert _text_slot(font_family=name).font_family == name


def test_textslot_rejects_size_min_over_size_max():
    with pytest.raises(ValidationError, match="size_min"):
        _text_slot(size_min=0.2, size_max=0.05)


def test_textslot_optional_slot_defaults_to_empty_content():
    slot = _text_slot(id="subtitle", optional=True)
    assert slot.content == ""
    assert slot.optional is True


# -- ArtSlot / ScrimSpec / LayerRef -------------------------------------------

def test_artslot_defaults():
    slot = ArtSlot(id="background")
    assert slot.prompt == ""
    assert slot.transparent is False
    assert slot.fit == "cover"
    assert slot.anchor == [0.5, 0.5]
    assert slot.offset == [0.0, 0.0]
    assert slot.opacity == 1.0
    assert slot.blend == "normal"
    assert slot.asset == ""


def test_scrimspec_strength_bounds():
    with pytest.raises(ValidationError):
        ScrimSpec(strength=1.5)
    with pytest.raises(ValidationError):
        ScrimSpec(strength=-0.1)
    assert ScrimSpec(strength=0.85).strength == 0.85


def test_layerref_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        LayerRef(kind="bogus", ref="title")


# -- RenderReport: every field required, no silent defaults -----------------

def test_render_report_requires_every_field():
    with pytest.raises(ValidationError):
        RenderReport(contrast={}, scrim_final={}, fitted_sizes={})   # no warnings


def test_render_report_accepts_full_data():
    report = RenderReport(contrast={"title": 5.2}, scrim_final={0: 0.4},
                          fitted_sizes={"title": 0.09}, warnings=[])
    assert report.contrast["title"] == 5.2
    assert report.scrim_final[0] == 0.4


# -- Direction / Directions: schema-enforced closed font list ---------------

def test_direction_rejects_unregistered_title_font():
    with pytest.raises(ValidationError):
        _direction(title_font="Comic Sans")


def test_direction_rejects_unregistered_author_font():
    with pytest.raises(ValidationError):
        _direction(author_font="Comic Sans")


def test_direction_requires_every_field_with_no_default():
    full = _direction().model_dump()
    for key in full:
        partial = {k: v for k, v in full.items() if k != key}
        with pytest.raises(ValidationError):
            Direction(**partial)


def test_direction_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Direction(**_direction().model_dump(), bogus="nope")


def test_directions_requires_at_least_one_concept():
    with pytest.raises(ValidationError):
        Directions(concepts=[])


def test_directions_rejects_more_than_six_concepts():
    with pytest.raises(ValidationError):
        Directions(concepts=[_direction() for _ in range(7)])


def test_directions_accepts_up_to_six_concepts():
    assert len(Directions(concepts=[_direction() for _ in range(6)]).concepts) == 6


# -- JobState / ConceptState (§8.1) ------------------------------------------

def test_jobstate_manuscript_fields_default_to_no_manuscript():
    job = JobState(job_id="20260828-abc123", brief=_brief(),
                   status="directing", created="2026-08-28T00:00:00Z")
    assert job.manuscript_name == ""
    assert job.word_count == 0
    assert job.concepts == []
    assert job.ledger == []


def test_jobstate_manuscript_fields_round_trip():
    job = JobState(job_id="20260828-abc123", brief=_brief(),
                   manuscript_name="draft.docx", word_count=88_412,
                   status="directing", created="2026-08-28T00:00:00Z")
    assert job.manuscript_name == "draft.docx"
    assert job.word_count == 88_412


def test_conceptstate_requires_a_status():
    spec = build_spec(_direction(), _brief(), ARCHETYPES["full_bleed_art"])
    with pytest.raises(ValidationError):
        ConceptState(spec=spec)


# -- build_spec: archetype + direction + brief -> CoverSpec ------------------

def test_build_spec_full_bleed_art_merges_everything():
    archetype = ARCHETYPES["full_bleed_art"]
    brief = _brief(title="The Lighthouse at Gull Point", subtitle="A Novel",
                   author="J. R. Vance", genre="literary")
    direction = _direction(
        archetype="full_bleed_art", title_font="Playfair Display",
        author_font="Spectral",
        art_prompts={"background": "A lonely lighthouse at dusk, oil painting."},
        texture=True)

    spec = build_spec(direction, brief, archetype)

    assert spec.archetype == "full_bleed_art"
    assert spec.concept_name == direction.concept_name
    assert spec.rationale == direction.rationale
    assert spec.palette == direction.palette
    assert spec.version == 1
    assert spec.notes_log == []

    by_id = {t.id: t for t in spec.text}
    assert by_id["title"].content == "The Lighthouse at Gull Point"
    assert by_id["title"].font_family == "Playfair Display"      # title_font
    assert by_id["subtitle"].content == "A Novel"
    assert by_id["subtitle"].font_family == "Spectral"           # author_font
    assert by_id["author"].content == "J. R. Vance"
    assert by_id["author"].font_family == "Spectral"             # author_font

    art_by_id = {a.id: a for a in spec.art}
    assert set(art_by_id) == {"background", "texture"}
    assert art_by_id["background"].prompt == \
        "A lonely lighthouse at dusk, oil painting."
    assert art_by_id["texture"].prompt == ""       # not generatable -> procedural

    assert [(l.kind, l.ref) for l in spec.layers] == [
        ("art", "background"), ("art", "texture"),
        ("scrim", "0"), ("scrim", "1"),
        ("text", "title"), ("text", "subtitle"), ("text", "author")]


def test_build_spec_skips_texture_when_direction_declines_it():
    archetype = ARCHETYPES["full_bleed_art"]
    direction = _direction(archetype="full_bleed_art", texture=False)
    spec = build_spec(direction, _brief(), archetype)
    assert {a.id for a in spec.art} == {"background"}
    assert ("art", "texture") not in [(l.kind, l.ref) for l in spec.layers]


def test_build_spec_rejects_a_mismatched_archetype():
    archetype = ARCHETYPES["big_type"]
    direction = _direction(archetype="full_bleed_art")   # names a different one
    with pytest.raises(ValueError, match="full_bleed_art"):
        build_spec(direction, _brief(), archetype)


def test_build_spec_cutout_sandwich_focal_is_transparent_contain_and_sandwiched():
    archetype = ARCHETYPES["cutout_sandwich"]
    direction = _direction(
        archetype="cutout_sandwich",
        art_prompts={"background": "A misty forest clearing, watercolor.",
                     "focal": "A cloaked figure, cutout subject only."})
    spec = build_spec(direction, _brief(), archetype)

    focal = next(a for a in spec.art if a.id == "focal")
    assert focal.transparent is True
    assert focal.fit == "contain"

    order = [(l.kind, l.ref) for l in spec.layers]
    assert (order.index(("art", "background")) < order.index(("text", "title"))
           < order.index(("art", "focal")) < order.index(("text", "author")))


def test_build_spec_big_type_title_matches_the_launch_spec():
    archetype = ARCHETYPES["big_type"]
    direction = _direction(archetype="big_type", texture=False)
    spec = build_spec(direction, _brief(), archetype)
    title = next(t for t in spec.text if t.id == "title")
    assert title.align == "left"
    assert title.size_max == pytest.approx(0.16)
    assert title.max_lines == 4
    assert {a.id for a in spec.art} == {"background"}   # texture declined


def test_build_spec_degrades_gracefully_for_a_series_slot_brief_has_no_field_for():
    # None of the 3 launch archetypes use "series" (Brief has no matching
    # field), but build_spec's content lookup is getattr(brief, slot.id, "")
    # specifically so a FUTURE archetype that adds one degrades to empty
    # content instead of raising AttributeError. Exercise that path directly
    # with a synthetic single-slot archetype built from the real loader's
    # own models (not a YAML file — Archetype validates just as strictly
    # either way).
    from docproof.cover.archetypes import (Archetype, ArchetypeArt,
                                           ArchetypeText, ArchetypeZone)
    synthetic = Archetype(
        name="synthetic", describe="test", composition_note="test",
        art=[ArchetypeArt(id="background", generatable=False)],
        text=[ArchetypeText(id="series",
                            zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.1),
                            size_min=0.02, size_max=0.05, optional=True)],
        layers=["background", "series"])
    direction = _direction(archetype="synthetic", art_prompts={}, texture=False)
    spec = build_spec(direction, _brief(), synthetic)
    series = next(t for t in spec.text if t.id == "series")
    assert series.content == ""
    assert series.font_family == direction.author_font
