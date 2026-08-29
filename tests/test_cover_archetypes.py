"""docproof/cover/archetypes.py (loading, zone math, layer resolution) and
docproof/cover/fonts.py (the font registry) — the two static-resource
foundation modules underneath docproof/cover/model.py.

No network. The "malformed file fails loudly" tests write real, broken YAML
to tmp_path and load it through the same code path config/cover/archetypes/
loads through at import — see docs/cover_designer_spec.md §5.1 and §11.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from docproof.cover.archetypes import (ARCHETYPES, ARCHETYPES_DIR,
                                       SUBJECT_KEYS, Archetype, ArchetypeArt,
                                       ArchetypeError, ArchetypeScrim,
                                       ArchetypeText, ArchetypeZone,
                                       describe_archetypes, load_archetypes,
                                       zone_px)
from docproof.cover.compose import compose
from docproof.cover.fonts import AUTHOR_FONT_DEFAULT, FAMILIES, describe_fonts, font_path
from docproof.cover.model import Brief, Direction, Palette, Zone, build_spec

# -- the three untagged launch archetypes, plus the §5.3 genre-tagged library
# grown from docs/cover_template_research.md, load and validate ------------

LAUNCH_ARCHETYPES = ("big_type", "cutout_sandwich", "full_bleed_art")

NEW_ARCHETYPES = (
    "romantasy_emblem", "scifi_geometric_object_minimal",
    "romance_flat_vector_couple", "romcom_maximalist_layered",
    "thriller_bigtype_silhouette", "cozy_mystery_graphic_stamp",
    "horror_dark_emblem_ornate", "historical_woman_walking_away",
    "literary_minimal_symbolic_object", "memoir_restrained_object_portrait",
    "nonfiction_bold_colorblock_typographic",
    "young_readers_character_illustration",
    "woven_emblem",
)

# Kept under its old name too — every existing test below that parametrizes
# on SHIPPED_ARCHETYPES gets the broader, still-valid claim "this holds for
# every shipped archetype", not just the original three.
SHIPPED_ARCHETYPES = LAUNCH_ARCHETYPES + NEW_ARCHETYPES


def test_every_shipped_archetype_loaded():
    assert set(ARCHETYPES) == set(SHIPPED_ARCHETYPES)


def test_archetypes_dir_holds_exactly_the_shipped_files():
    assert ARCHETYPES_DIR.is_dir()
    assert {p.stem for p in ARCHETYPES_DIR.glob("*.yaml")} == set(ARCHETYPES)


@pytest.mark.parametrize("name", SHIPPED_ARCHETYPES)
def test_shipped_archetype_is_a_valid_archetype_with_matching_name(name):
    archetype = ARCHETYPES[name]
    assert isinstance(archetype, Archetype)
    assert archetype.name == name
    assert archetype.describe.strip()
    assert archetype.composition_note.strip()
    assert archetype.art and archetype.text and archetype.layers


def test_big_type_has_no_focal_slot_and_a_procedural_background():
    big_type = ARCHETYPES["big_type"]
    # v2.1 BODY-fix wave added `rule_frame` (an always-procedural, never-
    # generatable double-rule) alongside the original background/texture —
    # still no focal slot at all, the point this test's own name makes.
    assert {a.id for a in big_type.art} == {"background", "texture", "rule_frame"}
    background = next(a for a in big_type.art if a.id == "background")
    assert background.generatable is False   # the $0-fallback guarantee
    rule_frame = next(a for a in big_type.art if a.id == "rule_frame")
    assert rule_frame.generatable is False
    assert rule_frame.procedural == "rule_frame"


def test_cutout_sandwich_focal_is_generatable_transparent_and_contained():
    cutout = ARCHETYPES["cutout_sandwich"]
    focal = next(a for a in cutout.art if a.id == "focal")
    assert focal.generatable is True
    assert focal.transparent is True
    assert focal.fit == "contain"


# -- layers references resolve (§11) -----------------------------------------

@pytest.mark.parametrize("name", SHIPPED_ARCHETYPES)
def test_every_layers_entry_resolves(name):
    archetype = ARCHETYPES[name]
    art_ids = {a.id for a in archetype.art}
    text_ids = {t.id for t in archetype.text}
    for ref in archetype.layers:
        if ref.startswith("scrim:"):
            assert int(ref.removeprefix("scrim:")) < len(archetype.scrims)
        else:
            assert ref in art_ids or ref in text_ids


def test_cutout_sandwich_layer_order_is_background_title_focal_author():
    order = ARCHETYPES["cutout_sandwich"].layers
    assert (order.index("background") < order.index("title")
           < order.index("focal") < order.index("author"))


# -- zone pct -> px helpers ---------------------------------------------------

def test_zone_px_full_canvas():
    zone = ArchetypeZone(x=0.0, y=0.0, w=1.0, h=1.0)
    assert zone_px(zone, (1600, 2560)) == (0, 0, 1600, 2560)


def test_zone_px_rounds_fractional_pixels():
    archetype = ARCHETYPES["full_bleed_art"]
    title = next(t for t in archetype.text if t.id == "title")   # x.08 y.62 w.84 h.22
    assert zone_px(title.zone, (1600, 2560)) == (128, 1587, 1344, 563)


def test_zone_px_is_structurally_typed_and_also_accepts_model_zone():
    # zone_px takes anything with x/y/w/h floats — a runtime
    # docproof.cover.model.Zone works exactly like an ArchetypeZone, with no
    # import of model.py needed inside archetypes.py itself.
    zone = Zone(x=0.25, y=0.25, w=0.5, h=0.5)
    assert zone_px(zone, (400, 640)) == (100, 160, 200, 320)


# -- describe_archetypes() ----------------------------------------------------

def test_describe_archetypes_mentions_every_archetype_and_its_describe_line():
    text = describe_archetypes()
    for archetype in ARCHETYPES.values():
        assert archetype.name in text
        assert " ".join(archetype.describe.split()) in text
    assert text.count("\n") == len(ARCHETYPES) - 1


# -- genres (§5.3): the field, its tags, and describe_archetypes(genre) ------

def test_launch_archetypes_remain_untagged():
    # DECIDED: the three launch archetypes fit every genre and are never
    # edited to add a genres list (docs/cover_designer_spec.md §5.3).
    for name in LAUNCH_ARCHETYPES:
        assert ARCHETYPES[name].genres == []


# One-line regression guard on every new archetype's actual tags, so a typo
# that still happens to be a VALID subject key (passing model validation)
# doesn't silently drift the library's genre coverage.
_EXPECTED_GENRES = {
    "romantasy_emblem": ["fantasy", "romance"],
    "scifi_geometric_object_minimal": ["science_fiction"],
    "romance_flat_vector_couple": ["romance"],
    "romcom_maximalist_layered": ["romance"],
    "thriller_bigtype_silhouette": ["mystery_thriller"],
    "cozy_mystery_graphic_stamp": ["mystery_thriller"],
    "horror_dark_emblem_ornate": ["horror", "romance"],
    "historical_woman_walking_away": ["historical"],
    "literary_minimal_symbolic_object": ["literary"],
    "memoir_restrained_object_portrait": ["memoir_biography"],
    "nonfiction_bold_colorblock_typographic": ["nonfiction"],
    "young_readers_character_illustration": ["young_readers"],
    "woven_emblem": ["fantasy", "romance", "literary", "historical"],
}


def test_new_archetypes_have_the_expected_genres():
    assert set(_EXPECTED_GENRES) == set(NEW_ARCHETYPES)
    for name, genres in _EXPECTED_GENRES.items():
        assert ARCHETYPES[name].genres == genres


def test_every_new_archetype_genre_is_a_subject_key():
    for name in NEW_ARCHETYPES:
        assert ARCHETYPES[name].genres, f"{name} should be genre-tagged"
        assert set(ARCHETYPES[name].genres) <= SUBJECT_KEYS


def test_subject_keys_has_exactly_the_ten_documented_keys():
    assert SUBJECT_KEYS == {
        "fantasy", "science_fiction", "romance", "mystery_thriller",
        "horror", "historical", "literary", "memoir_biography", "nonfiction",
        "young_readers"}


def test_archetype_rejects_an_unknown_genre_tag():
    with pytest.raises(ValidationError, match="not in the ten subject keys"):
        Archetype(name="x", describe="d", composition_note="c",
                 art=[ArchetypeArt(id="background", generatable=False)],
                 text=[ArchetypeText(id="title",
                                     zone=ArchetypeZone(x=0, y=0, w=0.5, h=0.5),
                                     size_min=0.02, size_max=0.1)],
                 layers=["background", "title"], genres=["not_a_real_genre"])


def test_archetype_genres_defaults_to_empty():
    archetype = Archetype(
        name="x", describe="d", composition_note="c",
        art=[ArchetypeArt(id="background", generatable=False)],
        text=[ArchetypeText(id="title",
                            zone=ArchetypeZone(x=0, y=0, w=0.5, h=0.5),
                            size_min=0.02, size_max=0.1)],
        layers=["background", "title"])
    assert archetype.genres == []


def test_describe_archetypes_genre_none_is_unfiltered():
    assert describe_archetypes(None) == describe_archetypes()


def test_describe_archetypes_genre_unknown_string_is_unfiltered():
    # A brief's genre is free text (docproof.cover.model.Brief) — a string
    # that isn't one of the ten subject keys must behave exactly like None,
    # not like "matches nothing".
    assert describe_archetypes("a genre nobody has heard of") == \
        describe_archetypes()
    assert describe_archetypes("") == describe_archetypes()


def test_describe_archetypes_genre_filters_to_tagged_plus_untagged():
    text = describe_archetypes("literary")
    for name in LAUNCH_ARCHETYPES:
        assert name in text                     # untagged: always included
    assert "literary_minimal_symbolic_object" in text   # tagged: matches
    # tagged for a DIFFERENT genre only, and not literary: excluded
    assert "nonfiction_bold_colorblock_typographic" not in text
    assert "thriller_bigtype_silhouette" not in text
    assert "young_readers_character_illustration" not in text


def test_describe_archetypes_genre_filter_includes_multi_genre_tags():
    # romantasy_emblem is tagged [fantasy, romance] — it must show up under
    # EITHER genre, not just the first one listed.
    assert "romantasy_emblem" in describe_archetypes("fantasy")
    assert "romantasy_emblem" in describe_archetypes("romance")
    # horror_dark_emblem_ornate is tagged [horror, romance] likewise.
    assert "horror_dark_emblem_ornate" in describe_archetypes("horror")
    assert "horror_dark_emblem_ornate" in describe_archetypes("romance")


def test_describe_archetypes_genre_filter_shrinks_the_enumeration():
    # A real assertion that filtering actually filters, not just includes.
    assert len(describe_archetypes("historical").splitlines()) < len(ARCHETYPES)


@pytest.mark.parametrize("genre", sorted(SUBJECT_KEYS))
def test_describe_archetypes_every_subject_key_yields_at_least_one_match(genre):
    # Every one of the ten genres has real coverage: at least the three
    # untagged launch archetypes, and for every genre represented in
    # _EXPECTED_GENRES, at least one purpose-built match too.
    text = describe_archetypes(genre)
    for name in LAUNCH_ARCHETYPES:
        assert name in text
    tagged_for_this_genre = [n for n, gs in _EXPECTED_GENRES.items()
                             if genre in gs]
    for name in tagged_for_this_genre:
        assert name in text


# -- every archetype: a valid CoverSpec, and a clean procedural compose -----

_SMALL_CANVAS = (400, 640)


def _direction_for(name: str) -> Direction:
    return Direction(
        concept_name="Test Concept", rationale="A test rationale.",
        archetype=name,
        palette=Palette(background="#20242c", primary="#f5f1e8",
                        accent="#c9a227", text="#f5f1e8", scrim="#000000"),
        title_font="Playfair Display", author_font="Spectral",
        art_prompts=[], texture=True)


@pytest.mark.parametrize("name", SHIPPED_ARCHETYPES)
def test_every_archetype_produces_a_valid_spec_via_build_spec(name):
    archetype = ARCHETYPES[name]
    brief = Brief(title="The Lighthouse at Gull Point", author="J. R. Vance",
                  genre="literary")
    spec = build_spec(_direction_for(name), brief, archetype)
    assert spec.archetype == name
    assert {t.id for t in spec.text} >= {"title", "author"}
    assert any(t.id == "title" and t.content == brief.title for t in spec.text)


@pytest.mark.parametrize("name", SHIPPED_ARCHETYPES)
def test_every_archetype_composes_cleanly_at_small_canvas(name):
    archetype = ARCHETYPES[name]
    brief = Brief(title="The Lighthouse at Gull Point", author="J. R. Vance",
                  genre="literary")
    spec = build_spec(_direction_for(name), brief, archetype)
    image, report = compose(spec, "/nonexistent-job-dir", canvas=_SMALL_CANVAS)
    assert image.size == _SMALL_CANVAS
    assert "title" in report.contrast
    assert "author" in report.contrast


# -- per-model validation (independent of any YAML file) ---------------------

def test_archetype_art_requires_the_generatable_flag():
    with pytest.raises(ValidationError):
        ArchetypeArt(id="background")   # generatable has no default


def test_archetype_text_rejects_size_min_over_size_max():
    with pytest.raises(ValidationError, match="size_min"):
        ArchetypeText(id="title", zone=ArchetypeZone(x=0, y=0, w=0.5, h=0.5),
                      size_min=0.2, size_max=0.05)


def test_archetype_zone_rejects_running_past_the_canvas():
    with pytest.raises(ValidationError):
        ArchetypeZone(x=0.7, y=0.1, w=0.5, h=0.1)


def test_archetype_scrim_strength_bounds():
    with pytest.raises(ValidationError):
        ArchetypeScrim(strength=1.5)


def test_archetype_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Archetype(name="x", describe="d", composition_note="c",
                 art=[ArchetypeArt(id="background", generatable=False)],
                 text=[ArchetypeText(id="title",
                                     zone=ArchetypeZone(x=0, y=0, w=0.5, h=0.5),
                                     size_min=0.02, size_max=0.1)],
                 layers=["background", "title"], bogus=True)


# -- malformed archetype files fail loudly, at load time ---------------------

_MINIMAL = """\
name: big_type
describe: "test archetype"
composition_note: "test note"
art:
  - id: background
    generatable: false
text:
  - id: title
    zone: {x: 0.1, y: 0.1, w: 0.8, h: 0.3}
    size_min: 0.02
    size_max: 0.1
layers: [background, title]
"""


def test_a_well_formed_minimal_archetype_loads(tmp_path):
    (tmp_path / "big_type.yaml").write_text(_MINIMAL)
    loaded = load_archetypes(tmp_path)
    assert set(loaded) == {"big_type"}
    assert loaded["big_type"].text[0].id == "title"


def test_not_a_mapping_fails_loudly(tmp_path):
    (tmp_path / "broken.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ArchetypeError, match="mapping"):
        load_archetypes(tmp_path)


def test_invalid_yaml_syntax_fails_loudly(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: [unterminated\n")
    with pytest.raises(ArchetypeError):
        load_archetypes(tmp_path)


def test_name_not_matching_file_name_fails_loudly(tmp_path):
    (tmp_path / "mismatch.yaml").write_text(_MINIMAL)   # declares name: big_type
    with pytest.raises(ArchetypeError, match="does not match"):
        load_archetypes(tmp_path)


def test_unresolvable_layer_reference_fails_loudly(tmp_path):
    broken = _MINIMAL.replace("layers: [background, title]",
                              "layers: [background, title, ghost]")
    (tmp_path / "big_type.yaml").write_text(broken)
    with pytest.raises(ArchetypeError, match="ghost"):
        load_archetypes(tmp_path)


def test_typod_genre_tag_fails_loudly(tmp_path):
    # §5.3: "a typo'd genre tag must fail loudly at load" — the exact
    # scenario a template author hits when they write "fantsy" or "YA".
    broken = _MINIMAL + "genres: [fantsy]\n"
    (tmp_path / "big_type.yaml").write_text(broken)
    with pytest.raises(ArchetypeError, match="fantsy"):
        load_archetypes(tmp_path)


def test_valid_genre_tags_load_fine(tmp_path):
    ok = _MINIMAL + "genres: [fantasy, romance]\n"
    (tmp_path / "big_type.yaml").write_text(ok)
    loaded = load_archetypes(tmp_path)
    assert loaded["big_type"].genres == ["fantasy", "romance"]


def test_out_of_range_scrim_index_fails_loudly(tmp_path):
    broken = _MINIMAL.replace("layers: [background, title]",
                              'layers: [background, "scrim:0", title]')
    (tmp_path / "big_type.yaml").write_text(broken)
    with pytest.raises(ArchetypeError, match="scrim:0"):
        load_archetypes(tmp_path)


def test_duplicate_art_id_fails_loudly(tmp_path):
    broken = _MINIMAL.replace(
        "art:\n  - id: background\n    generatable: false\n",
        "art:\n  - id: background\n    generatable: false\n"
        "  - id: background\n    generatable: true\n")
    (tmp_path / "big_type.yaml").write_text(broken)
    with pytest.raises(ArchetypeError, match="duplicate"):
        load_archetypes(tmp_path)


def test_empty_directory_fails_loudly(tmp_path):
    with pytest.raises(ArchetypeError):
        load_archetypes(tmp_path)


def test_missing_directory_fails_loudly(tmp_path):
    with pytest.raises(ArchetypeError):
        load_archetypes(tmp_path / "does-not-exist")


# -- fonts.py: the registry ---------------------------------------------------

_EXPECTED_FAMILIES = {
    "Spectral", "IM FELL English", "EB Garamond", "Playfair Display",
    "Cormorant Garamond", "Lora", "Quicksand", "Orbitron", "Special Elite",
    "Pirata One",
}


def test_families_has_exactly_the_ten_registered_names():
    assert set(FAMILIES) == _EXPECTED_FAMILIES
    assert len(FAMILIES) == 10


def test_every_family_resolves_to_a_real_ttf_on_disk():
    for name in FAMILIES:
        path = font_path(name)
        assert path.is_file(), f"{name}: no file at {path}"
        assert path.suffix == ".ttf"


def test_every_family_carries_its_own_name_and_a_vibe_line():
    for name, font in FAMILIES.items():
        assert font.family == name
        assert isinstance(font.vibe, str) and font.vibe.strip()
        assert isinstance(font.caps_friendly, bool)


def test_font_path_rejects_an_unregistered_family():
    with pytest.raises(KeyError):
        font_path("Comic Sans")


def test_author_font_default_is_itself_a_registered_family():
    assert AUTHOR_FONT_DEFAULT in FAMILIES


def test_describe_fonts_mentions_every_family_and_its_vibe():
    text = describe_fonts()
    for name, font in FAMILIES.items():
        assert name in text
        assert font.vibe in text


def test_archetype_art_rejects_a_malformed_anchor_pair():
    # The module promises malformed archetype data fails LOUDLY at load —
    # that must include the placement pairs, or a bad YAML only explodes
    # later inside build_spec, in a detached job task.
    with pytest.raises(ValidationError, match="anchor/offset"):
        ArchetypeArt(id="focal", generatable=True, anchor=[0.5])
    with pytest.raises(ValidationError, match="anchor/offset"):
        ArchetypeArt(id="focal", generatable=True, offset=[0.0, 9.0])


# ===========================================================================
# v2 BODY wave: free-form art slot ids, procedural synthesizers, and
# TextSlot-inside-art (mask_from) at the archetype layer
# ===========================================================================

@pytest.mark.parametrize("slot_id", [
    "background", "focal", "focal2", "foreground", "texture",   # legacy five
    "vine_left", "emblem", "border_motif", "weave", "corner_vine",
])
def test_archetype_art_id_accepts_any_valid_slug(slot_id):
    assert ArchetypeArt(id=slot_id, generatable=False).id == slot_id


@pytest.mark.parametrize("slot_id", [
    "", "Emblem", "vine-left", "vine left", "1corner", "z" * 25,
])
def test_archetype_art_id_rejects_invalid_slugs(slot_id):
    with pytest.raises(ValidationError, match="valid art slot id"):
        ArchetypeArt(id=slot_id, generatable=False)


def test_archetype_art_procedural_defaults_to_off():
    assert ArchetypeArt(id="paper", generatable=False).procedural == ""


@pytest.mark.parametrize("name", ["gradient", "grain", "paper", "halftone",
                                  "canvas", "speckle", "rule_frame"])
def test_archetype_art_procedural_accepts_every_documented_synthesizer(name):
    art = ArchetypeArt(id="paper", generatable=False, procedural=name)
    assert art.procedural == name


def test_archetype_art_procedural_rejects_an_undocumented_name():
    with pytest.raises(ValidationError):
        ArchetypeArt(id="paper", generatable=False, procedural="sparkles")


def test_archetype_text_mask_from_defaults_to_off():
    text = ArchetypeText(id="title", zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                         size_min=0.05, size_max=0.1)
    assert text.mask_from == ""


def test_archetype_text_mask_from_exists_dangling_reference_fails_loudly():
    with pytest.raises(ValidationError, match="mask_from"):
        Archetype(name="x", describe="x", composition_note="x",
                 art=[ArchetypeArt(id="beam", generatable=True)],
                 text=[ArchetypeText(id="title",
                                     zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                                     size_min=0.05, size_max=0.1,
                                     mask_from="nonexistent")],
                 layers=["beam", "title"])


def test_archetype_text_mask_from_valid_reference_loads_fine():
    archetype = Archetype(
        name="x", describe="x", composition_note="x",
        art=[ArchetypeArt(id="beam", generatable=True)],
        text=[ArchetypeText(id="title",
                            zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                            size_min=0.05, size_max=0.1, mask_from="beam")],
        layers=["beam", "title"])
    assert archetype.text[0].mask_from == "beam"


def test_archetype_text_mask_from_does_not_need_to_precede_in_layers():
    # Mirrors CoverSpec's own no-ordering-required rule (model.py's
    # _text_mask_from_resolves) — the container may be declared to draw
    # AFTER the text it clips.
    archetype = Archetype(
        name="x", describe="x", composition_note="x",
        art=[ArchetypeArt(id="beam", generatable=True)],
        text=[ArchetypeText(id="title",
                            zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                            size_min=0.05, size_max=0.1, mask_from="beam")],
        layers=["title", "beam"])
    assert archetype.text[0].mask_from == "beam"


# -- woven_emblem: the v2 BODY wave flagship ---------------------------------

def test_woven_emblem_declares_the_designed_slot_vocabulary():
    archetype = ARCHETYPES["woven_emblem"]
    assert {a.id for a in archetype.art} == {
        "background", "paper", "rule_frame", "corner_vine", "emblem", "weave"}
    assert {t.id for t in archetype.text} == {
        "series", "title", "subtitle", "author"}


def test_woven_emblem_procedural_slots_use_the_documented_synthesizers():
    art_by_id = {a.id: a for a in ARCHETYPES["woven_emblem"].art}
    assert art_by_id["background"].procedural == "gradient"
    assert art_by_id["paper"].procedural == "paper"
    assert art_by_id["rule_frame"].procedural == "rule_frame"
    for slot_id in ("background", "paper", "rule_frame"):
        assert art_by_id[slot_id].generatable is False


def test_woven_emblem_ornament_slots_are_tone_on_tone_silhouette():
    # Reference DNA #5: silhouette/duotone illustration, never a raw
    # full-color render, on every generated ornament.
    art_by_id = {a.id: a for a in ARCHETYPES["woven_emblem"].art}
    for slot_id in ("corner_vine", "emblem", "weave"):
        assert art_by_id[slot_id].treatment == "silhouette"
        assert art_by_id[slot_id].generatable is True
        assert art_by_id[slot_id].transparent is True


def test_woven_emblem_corner_vine_mirrors_without_touching_the_emblem():
    art_by_id = {a.id: a for a in ARCHETYPES["woven_emblem"].art}
    assert art_by_id["corner_vine"].corners is True
    assert art_by_id["emblem"].corners is False


def test_woven_emblem_title_is_huge_and_bottom_anchored():
    # Reference DNA #2: type is the hero (size_max clears the 0.13 floor);
    # valign bottom is what makes `weave`'s fixed position reliably cross
    # the title's last line regardless of how many lines it needs.
    title = next(t for t in ARCHETYPES["woven_emblem"].text if t.id == "title")
    assert title.size_max >= 0.13
    assert title.max_lines == 4
    assert title.valign == "bottom"


def test_woven_emblem_weave_is_drawn_after_title_in_layer_order():
    # Reference DNA #3: the interweave signature — an ornament crossing
    # back OVER the title's own lower edge only works if it is drawn later.
    layers = ARCHETYPES["woven_emblem"].layers
    assert layers.index("title") < layers.index("weave")


def test_woven_emblem_scrims_default_to_the_local_panel_kind_at_zero_strength():
    # De-muted by design: strength 0 means nothing dims unless the
    # legibility autopilot actually measures a problem.
    archetype = ARCHETYPES["woven_emblem"]
    assert len(archetype.scrims) == 2
    for scrim in archetype.scrims:
        assert scrim.kind == "panel"
        assert scrim.strength == 0.0
    assert {s.protects for s in archetype.scrims} == {"title", "author"}
