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
                                       ArchetypeError, ArchetypeGradientMask,
                                       ArchetypeMask, ArchetypeScrim,
                                       ArchetypeText, ArchetypeZone,
                                       describe_archetypes, load_archetypes,
                                       zone_px)
from docproof.cover.compose import compose
from docproof.cover.recipes import RECIPES
from docproof.cover.fonts import AUTHOR_FONT_DEFAULT, FAMILIES, describe_fonts, font_path
from docproof.cover.model import Brief, Direction, Palette, Zone, build_spec

import cover_probes

# -- the three untagged launch archetypes, plus the §5.3 genre-tagged library
# grown from docs/cover_template_research.md, load and validate ------------

# THIS FILE is the one that asserts SHIPPED CONTENT. Every other cover test
# reaches for a `probe_*` fixture archetype instead (see tests/conftest.py) so
# that retiring or adding a template can never again break a third of a suite
# that has nothing to do with templates.
#
# The probes ARE in the live registry, so anything here that reasons about
# "the shelf" has to subtract them.
SHIPPED_ARCHETYPES = tuple(
    sorted(set(ARCHETYPES) - set(cover_probes.PROBE_ARCHETYPES)))


def test_the_shelf_is_exactly_what_is_on_disk():
    """The registry and the directory agree, probes excluded. Deliberately
    NOT a hardcoded roster: a list of names in a test is a second place to
    remember, and it was the thing that broke every time the shelf moved."""
    assert ARCHETYPES_DIR.is_dir()
    on_disk = {p.stem for p in ARCHETYPES_DIR.glob("*.yaml")}
    assert on_disk == set(SHIPPED_ARCHETYPES)
    assert on_disk, "the shelf must not be empty"


def test_probe_archetypes_are_not_shipped():
    """The fixtures live under tests/, never in the shipped directory."""
    on_disk = {p.stem for p in ARCHETYPES_DIR.glob("*.yaml")}
    assert not (on_disk & set(cover_probes.PROBE_ARCHETYPES))


@pytest.mark.parametrize("name", SHIPPED_ARCHETYPES)
def test_shipped_archetype_is_a_valid_archetype_with_matching_name(name):
    archetype = ARCHETYPES[name]
    assert isinstance(archetype, Archetype)
    assert archetype.name == name
    assert archetype.describe.strip()
    assert archetype.composition_note.strip()
    assert archetype.art and archetype.text and archetype.layers


def test_probe_typographic_has_no_focal_slot_and_a_procedural_background():
    probe = ARCHETYPES["probe_typographic"]
    # v2.1 BODY-fix wave added `rule_frame` (an always-procedural, never-
    # generatable double-rule) alongside the original background/texture —
    # still no focal slot at all, the point this test's own name makes.
    assert {a.id for a in probe.art} == {"background", "texture", "rule_frame"}
    background = next(a for a in probe.art if a.id == "background")
    assert background.generatable is False   # the $0-fallback guarantee
    rule_frame = next(a for a in probe.art if a.id == "rule_frame")
    assert rule_frame.generatable is False
    assert rule_frame.procedural == "rule_frame"


def test_probe_sandwich_focal_is_generatable_transparent_and_contained():
    cutout = ARCHETYPES["probe_sandwich"]
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
    adjust_ids = {a.id for a in archetype.adjust}
    for ref in archetype.layers:
        if ref.startswith("scrim:"):
            assert int(ref.removeprefix("scrim:")) < len(archetype.scrims)
        else:
            assert ref in art_ids or ref in text_ids or ref in adjust_ids


def test_probe_sandwich_layer_order_is_background_title_focal_author():
    order = ARCHETYPES["probe_sandwich"].layers
    assert (order.index("background") < order.index("title")
           < order.index("focal") < order.index("author"))


# -- zone pct -> px helpers ---------------------------------------------------

def test_zone_px_full_canvas():
    zone = ArchetypeZone(x=0.0, y=0.0, w=1.0, h=1.0)
    assert zone_px(zone, (1600, 2560)) == (0, 0, 1600, 2560)


def test_zone_px_rounds_fractional_pixels():
    archetype = ARCHETYPES["probe_scene"]
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

def test_an_untagged_archetype_is_in_scope_for_every_genre():
    # §5.3: an archetype with no `genres` list fits every genre. Asserted as
    # the RULE rather than as a list of names that fit it — the old version
    # pinned the three launch archetypes, and retiring them took the rule's
    # only coverage with them.
    untagged = [n for n, a in ARCHETYPES.items() if not a.genres]
    assert untagged, "the probe fixtures supply the untagged cases"
    for genre in sorted(SUBJECT_KEYS):
        text = describe_archetypes(genre)
        for name in untagged:
            assert name in text


# One-line regression guard on every new archetype's actual tags, so a typo
# that still happens to be a VALID subject key (passing model validation)
# doesn't silently drift the library's genre coverage.
_EXPECTED_GENRES = {
    "romantasy_organic": ["fantasy", "romance"],
    "romantasy_enclosure": ["fantasy", "romance"],
}


def test_new_archetypes_have_the_expected_genres():
    assert set(_EXPECTED_GENRES) == set(SHIPPED_ARCHETYPES)
    for name, genres in _EXPECTED_GENRES.items():
        assert ARCHETYPES[name].genres == genres


def test_every_new_archetype_genre_is_a_subject_key():
    for name in SHIPPED_ARCHETYPES:
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
    """The §5.3 rule, asserted against whatever is actually loaded rather
    than against remembered names: an UNTAGGED archetype always appears, a
    tagged one appears only under a genre it names."""
    untagged = [n for n, a in ARCHETYPES.items() if not a.genres]
    assert untagged, "the probe fixtures supply the untagged cases"
    tagged = {n: a.genres for n, a in ARCHETYPES.items() if a.genres}
    assert tagged, "the shelf supplies the tagged cases"
    for genre in sorted({g for gs in tagged.values() for g in gs}):
        text = describe_archetypes(genre)
        for name in untagged:
            assert name in text, f"untagged {name} missing from {genre}"
        for name, genres in tagged.items():
            if genre in genres:
                assert name in text
            else:
                assert name not in text


def test_describe_archetypes_genre_filter_includes_multi_genre_tags():
    """A multi-genre archetype shows up under EVERY genre it names, not just
    the first one listed."""
    multi = {n: a.genres for n, a in ARCHETYPES.items() if len(a.genres) > 1}
    assert multi, "at least one shipped archetype should be multi-genre"
    for name, genres in multi.items():
        for genre in genres:
            assert name in describe_archetypes(genre), (name, genre)


def test_describe_archetypes_genre_filter_shrinks_the_enumeration():
    # A real assertion that filtering actually filters, not just includes.
    assert len(describe_archetypes("historical").splitlines()) < len(ARCHETYPES)


@pytest.mark.parametrize("genre", sorted(SUBJECT_KEYS))
def test_describe_archetypes_every_subject_key_yields_at_least_one_match(genre):
    # Every one of the ten genres has real coverage: at least the three
    # untagged launch archetypes, and for every genre represented in
    # _EXPECTED_GENRES, at least one purpose-built match too.
    text = describe_archetypes(genre)
    untagged = [n for n, a in ARCHETYPES.items() if not a.genres]
    for name in untagged:
        assert name in text
    for name in [n for n, gs in _EXPECTED_GENRES.items() if genre in gs]:
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


# -- the two mask-forward archetypes (§15.13 part 3) --------------------------

def test_probe_glyphmask_is_an_art_fill_title_with_art_clipped_into_the_glyphs():
    archetype = ARCHETYPES["probe_glyphmask"]
    title = next(t for t in archetype.text if t.id == "title")
    assert title.mode == "art_fill"          # glyphs as a window (§7.4a)
    window = next(a for a in archetype.art if a.id == "window_art")
    assert window.generatable is True
    assert window.fit == "cover"             # full bleed behind the glyphs
    assert window.mask is not None
    assert window.mask.from_text == "title"  # §15.13 part 1: text as clip
    assert archetype.recipe == "quiet_literary"   # the quiet finishing recipe
    assert archetype.axis == "center"
    # window_art must be drawn BEFORE the title so the art_fill ring lands
    # on top of the clipped art's edges.
    order = archetype.layers
    assert order.index("window_art") < order.index("title")


def test_probe_seam_gradient_masks_plate_b_into_plate_a_with_type_on_the_seam():
    archetype = ARCHETYPES["probe_seam"]
    plates = [a for a in archetype.art if a.generatable]
    assert {p.id for p in plates} == {"background", "plate_lower"}
    lower = next(a for a in archetype.art if a.id == "plate_lower")
    assert lower.mask is not None and lower.mask.gradient is not None
    gradient = lower.mask.gradient
    assert gradient.kind == "linear"        # the two-plate collage move (§15.2)
    # Type on the seam: the title's zone lies inside the mask's own
    # dissolve band, so the words always sit where the plates blend.
    title = next(t for t in archetype.text if t.id == "title")
    assert gradient.start <= title.zone.y
    assert title.zone.y + title.zone.h <= gradient.end + 1e-6
    assert archetype.recipe == "cinematic_duotone"
    order = archetype.layers
    assert order.index("background") < order.index("plate_lower") \
        < order.index("title")


@pytest.mark.parametrize("name", ("probe_glyphmask", "probe_seam"))
def test_mask_forward_archetype_builds_a_spec_carrying_its_masks(name):
    # The archetype-authored mask must ride into the BUILT CoverSpec (the
    # new ArchetypeArt.mask -> ArtSlot.mask pass-through in build_spec) —
    # this is what makes the mask machinery actually reachable from YAML.
    archetype = ARCHETYPES[name]
    brief = Brief(title="The Lighthouse at Gull Point", author="J. R. Vance",
                  genre="literary")
    spec = build_spec(_direction_for(name), brief, archetype)
    masked = {a.id: a.mask for a in spec.art if a.mask is not None}
    if name == "probe_glyphmask":
        assert masked["window_art"].from_text == "title"
    else:
        assert masked["plate_lower"].gradient is not None
        assert masked["plate_lower"].gradient.kind == "linear"
    # The finishing recipe expanded into real fx_ layers (§15.6).
    assert any(a.id.startswith("fx_") for a in spec.art) or spec.adjust
    assert spec.axis == "center"


@pytest.mark.parametrize("name", ("probe_glyphmask", "probe_seam"))
def test_mask_forward_archetype_procedural_render_is_green(name):
    # §15.13's own test bullet: both templates procedural-render green
    # through the legibility autopilot and the balance pass — no dead
    # band, no left/right balance flag, contrast measured for every
    # required slot, before a single image dollar is spent.
    archetype = ARCHETYPES[name]
    brief = Brief(title="The Lighthouse at Gull Point", author="J. R. Vance",
                  genre="literary")
    spec = build_spec(_direction_for(name), brief, archetype)
    image, report = compose(spec, "/nonexistent-job-dir", canvas=_SMALL_CANVAS)
    assert image.size == _SMALL_CANVAS
    assert "title" in report.contrast and "author" in report.contrast
    assert not any("empty band" in w for w in report.warnings)
    assert not any("left/right balance" in w for w in report.warnings)


# -- ArchetypeMask / ArchetypeGradientMask (the §15.13 YAML enabler) ---------

def _one_slot_archetype(**art_overrides) -> dict:
    """Kwargs for a minimal two-art-slot archetype, with `base` drawn
    before `over` — the shape every mask test below perturbs."""
    over = dict(id="over", generatable=True)
    over.update(art_overrides)
    return dict(
        name="x", describe="d", composition_note="c",
        art=[ArchetypeArt(id="base", generatable=False),
             ArchetypeArt.model_validate(over)],
        text=[ArchetypeText(id="title",
                            zone=ArchetypeZone(x=0, y=0, w=0.5, h=0.5),
                            size_min=0.02, size_max=0.1)],
        layers=["base", "over", "title"])


def test_archetype_art_accepts_a_first_class_mask():
    archetype = Archetype(**_one_slot_archetype(
        mask={"from_text": "title"}))
    over = next(a for a in archetype.art if a.id == "over")
    assert over.mask is not None and over.mask.from_text == "title"


def test_archetype_art_rejects_mask_and_mask_from_together():
    with pytest.raises(ValidationError, match="both mask_from and mask"):
        ArchetypeArt(id="over", generatable=True, mask_from="base",
                     mask=ArchetypeMask(from_layer="base"))


def test_archetype_mask_requires_at_least_one_source():
    with pytest.raises(ValidationError, match="no source"):
        ArchetypeMask()


def test_archetype_gradient_mask_rejects_a_reversed_ramp():
    with pytest.raises(ValidationError, match="strictly less"):
        ArchetypeGradientMask(start=0.7, end=0.3)


def test_archetype_mask_from_text_must_name_a_real_text_slot():
    with pytest.raises(ValidationError, match="not one of this archetype's "
                                              "text slots"):
        Archetype(**_one_slot_archetype(mask={"from_text": "subtitle"}))


def test_archetype_mask_from_layer_must_precede_the_masked_slot():
    # `over` clipped to `base` (drawn first) is fine; `base` clipped to
    # `over` (drawn later) violates the from_layer ordering rule and must
    # fail at LOAD, not three modules later at build_spec.
    Archetype(**_one_slot_archetype(mask={"from_layer": "base"}))
    kwargs = _one_slot_archetype()
    kwargs["art"] = [
        ArchetypeArt(id="base", generatable=False,
                     mask=ArchetypeMask(from_layer="over")),
        ArchetypeArt(id="over", generatable=True)]
    with pytest.raises(ValidationError, match="must appear earlier"):
        Archetype(**kwargs)


def test_archetype_mask_from_layer_must_name_a_real_art_slot():
    with pytest.raises(ValidationError, match="not one of this archetype's "
                                              "art slots"):
        Archetype(**_one_slot_archetype(mask={"from_layer": "nope"}))


def test_archetype_mask_from_text_cycle_refused():
    # `over` clipped INTO the title's glyphs while the title is itself
    # clipped to `over` — CoverSpec's one true from_text cycle, refused at
    # archetype load too.
    kwargs = _one_slot_archetype(mask={"from_text": "title"})
    kwargs["text"] = [ArchetypeText(
        id="title", zone=ArchetypeZone(x=0, y=0, w=0.5, h=0.5),
        size_min=0.02, size_max=0.1, mask_from="over")]
    with pytest.raises(ValidationError, match="cycle"):
        Archetype(**kwargs)


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
name: loader_probe
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
    (tmp_path / "loader_probe.yaml").write_text(_MINIMAL)
    loaded = load_archetypes(tmp_path)
    assert set(loaded) == {"loader_probe"}
    assert loaded["loader_probe"].text[0].id == "title"


def test_not_a_mapping_fails_loudly(tmp_path):
    (tmp_path / "broken.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ArchetypeError, match="mapping"):
        load_archetypes(tmp_path)


def test_invalid_yaml_syntax_fails_loudly(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: [unterminated\n")
    with pytest.raises(ArchetypeError):
        load_archetypes(tmp_path)


def test_name_not_matching_file_name_fails_loudly(tmp_path):
    (tmp_path / "mismatch.yaml").write_text(_MINIMAL)   # declares name: loader_probe
    with pytest.raises(ArchetypeError, match="does not match"):
        load_archetypes(tmp_path)


def test_unresolvable_layer_reference_fails_loudly(tmp_path):
    broken = _MINIMAL.replace("layers: [background, title]",
                              "layers: [background, title, ghost]")
    (tmp_path / "loader_probe.yaml").write_text(broken)
    with pytest.raises(ArchetypeError, match="ghost"):
        load_archetypes(tmp_path)


def test_typod_genre_tag_fails_loudly(tmp_path):
    # §5.3: "a typo'd genre tag must fail loudly at load" — the exact
    # scenario a template author hits when they write "fantsy" or "YA".
    broken = _MINIMAL + "genres: [fantsy]\n"
    (tmp_path / "loader_probe.yaml").write_text(broken)
    with pytest.raises(ArchetypeError, match="fantsy"):
        load_archetypes(tmp_path)


def test_valid_genre_tags_load_fine(tmp_path):
    ok = _MINIMAL + "genres: [fantasy, romance]\n"
    (tmp_path / "loader_probe.yaml").write_text(ok)
    loaded = load_archetypes(tmp_path)
    assert loaded["loader_probe"].genres == ["fantasy", "romance"]


def test_out_of_range_scrim_index_fails_loudly(tmp_path):
    broken = _MINIMAL.replace("layers: [background, title]",
                              'layers: [background, "scrim:0", title]')
    (tmp_path / "loader_probe.yaml").write_text(broken)
    with pytest.raises(ArchetypeError, match="scrim:0"):
        load_archetypes(tmp_path)


def test_duplicate_art_id_fails_loudly(tmp_path):
    broken = _MINIMAL.replace(
        "art:\n  - id: background\n    generatable: false\n",
        "art:\n  - id: background\n    generatable: false\n"
        "  - id: background\n    generatable: true\n")
    (tmp_path / "loader_probe.yaml").write_text(broken)
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


def test_families_still_carries_the_ten_launch_names():
    # The §15.11 expansion grew the shelf past ten, but the launch names are
    # model-visible API (archived directions refer to them) and must survive
    # verbatim. The expansion roster itself is covered in test_cover_fonts.py.
    assert _EXPECTED_FAMILIES <= set(FAMILIES)
    assert len(FAMILIES) >= 10


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

def test_probe_ornament_declares_the_designed_slot_vocabulary():
    archetype = ARCHETYPES["probe_ornament"]
    assert {a.id for a in archetype.art} == {
        "background", "paper", "rule_frame", "corner_vine", "emblem", "weave"}
    assert {t.id for t in archetype.text} == {
        "series", "title", "subtitle", "author"}


def test_probe_ornament_procedural_slots_use_the_documented_synthesizers():
    art_by_id = {a.id: a for a in ARCHETYPES["probe_ornament"].art}
    assert art_by_id["background"].procedural == "gradient"
    assert art_by_id["paper"].procedural == "paper"
    assert art_by_id["rule_frame"].procedural == "rule_frame"
    for slot_id in ("background", "paper", "rule_frame"):
        assert art_by_id[slot_id].generatable is False


def test_probe_ornament_ornament_slots_are_tone_on_tone_silhouette():
    # Reference DNA #5: silhouette/duotone illustration, never a raw
    # full-color render, on every generated ornament.
    art_by_id = {a.id: a for a in ARCHETYPES["probe_ornament"].art}
    for slot_id in ("corner_vine", "emblem", "weave"):
        assert art_by_id[slot_id].treatment == "silhouette"
        assert art_by_id[slot_id].generatable is True
        assert art_by_id[slot_id].transparent is True


def test_probe_ornament_corner_vine_mirrors_without_touching_the_emblem():
    art_by_id = {a.id: a for a in ARCHETYPES["probe_ornament"].art}
    assert art_by_id["corner_vine"].corners is True
    assert art_by_id["emblem"].corners is False


def test_probe_ornament_title_is_huge_and_bottom_anchored():
    # Reference DNA #2: type is the hero (size_max clears the 0.13 floor);
    # valign bottom is what makes `weave`'s fixed position reliably cross
    # the title's last line regardless of how many lines it needs.
    title = next(t for t in ARCHETYPES["probe_ornament"].text if t.id == "title")
    assert title.size_max >= 0.13
    assert title.max_lines == 4
    assert title.valign == "bottom"


def test_probe_ornament_weave_is_drawn_after_title_in_layer_order():
    # Reference DNA #3: the interweave signature — an ornament crossing
    # back OVER the title's own lower edge only works if it is drawn later.
    layers = ARCHETYPES["probe_ornament"].layers
    assert layers.index("title") < layers.index("weave")


def test_probe_ornament_scrims_default_to_the_local_panel_kind_at_zero_strength():
    # De-muted by design: strength 0 means nothing dims unless the
    # legibility autopilot actually measures a problem.
    archetype = ARCHETYPES["probe_ornament"]
    assert len(archetype.scrims) == 2
    for scrim in archetype.scrims:
        assert scrim.kind == "panel"
        assert scrim.strength == 0.0
    assert {s.protects for s in archetype.scrims} == {"title", "author"}


# ===========================================================================
# Deep-stack wave, PR4: recipe defaults, effect stacks, the fx_ reservation
# ===========================================================================

def test_a_named_default_recipe_is_on_the_shelf_and_expands():
    """§15.6: a template's default `recipe` must name a real shelf entry,
    and `recipe_strength` must be a fraction of it.

    This replaces a test that pinned WHICH THREE archetypes wore a recipe.
    That assertion was pure content bookkeeping — it broke the moment the
    shelf changed, and it never protected the thing that actually matters,
    which is that whatever a template names resolves and expands."""
    for name in SHIPPED_ARCHETYPES:
        archetype = ARCHETYPES[name]
        if not archetype.recipe:
            continue
        assert archetype.recipe in RECIPES, name
        assert 0.0 <= archetype.recipe_strength <= 1.0, name


def test_probes_cover_both_sides_of_the_recipe_default():
    """The fixture set keeps a template WITH a default recipe and one
    without, so both branches of build_spec's recipe fallback stay covered
    no matter what the shipped shelf happens to look like."""
    recipes = {n: ARCHETYPES[n].recipe for n in cover_probes.PROBE_ARCHETYPES}
    assert any(recipes.values()), recipes
    assert any(not r for r in recipes.values()), recipes


def test_probe_typestack_title_carries_the_stacked_double_shadow():
    title = next(t for t in ARCHETYPES["probe_typestack"].text
                 if t.id == "title")
    assert [e.kind for e in title.effects] == ["drop_shadow", "drop_shadow"]
    wide, tight = title.effects
    # Stack order is paint order: the wide ambient wash first (deepest),
    # the tight contact edge over it.
    assert wide.blur > tight.blur
    assert wide.dy > tight.dy
    assert tight.alpha > wide.alpha


def test_retrofitted_typestack_spec_folds_the_stack_through_build_spec():
    direction = Direction(
        concept_name="Test", rationale="test",
        archetype="probe_typestack",
        palette=Palette(background="#101820", primary="#c9382c",
                        accent="#c9a227", text="#f5f1e8", scrim="#000000"),
        title_font="Spectral", author_font="Spectral", art_prompts=[],
        texture=True)
    spec = build_spec(direction, Brief(title="Ash", author="V.", genre="literary"),
                      ARCHETYPES["probe_typestack"])
    title = next(t for t in spec.text if t.id == "title")
    assert [e.kind for e in title.effects] == ["drop_shadow", "drop_shadow"]


def test_fx_prefix_is_reserved_against_hand_authored_slots():
    # §15.6: recipe-expanded layers own the prefix; an archetype slot may
    # never wear it, so an expansion can never collide by construction.
    with pytest.raises(ValidationError, match="reserved"):
        ArchetypeArt(id="fx_glow", generatable=False)


def test_fx_prefix_rejection_reaches_a_yaml_load(tmp_path):
    (tmp_path / "sneaky.yaml").write_text(
        "name: sneaky\ndescribe: test\ncomposition_note: test\n"
        "art:\n  - id: fx_wash\n    generatable: false\n"
        "text:\n  - id: title\n    zone: {x: 0.1, y: 0.1, w: 0.8, h: 0.2}\n"
        "    size_min: 0.04\n    size_max: 0.08\n"
        "layers: [fx_wash, title]\n", encoding="utf-8")
    with pytest.raises(ArchetypeError, match="reserved"):
        load_archetypes(tmp_path)


@pytest.mark.parametrize("name", ["probe_typographic", "probe_scene",
                                  "probe_typestack"])
def test_retrofitted_default_render_passes_autopilot_and_balance(name):
    # Each PR4 retrofit's DEFAULT path (silent direction), rendered
    # procedurally: every slot's final contrast clears its threshold and
    # neither the draw-time autopilot nor the §15.7 re-check gave up. The
    # balance measurements ran report-only (no axis declared → no snaps).
    from pathlib import Path
    from docproof.cover.compose import _CONTRAST_THRESHOLDS
    direction = Direction(
        concept_name="Test", rationale="test", archetype=name,
        palette=Palette(background="#101820", primary="#c9382c",
                        accent="#c9a227", text="#f5f1e8", scrim="#000000"),
        title_font="Spectral", author_font="Spectral", art_prompts=[],
        texture=True)
    spec = build_spec(direction, Brief(title="The Lighthouse at Gull Point",
                                       subtitle="A Novel", author="J. R. Vance",
                                       genre="literary"), ARCHETYPES[name])
    _, report = compose(spec, Path("/nonexistent"), canvas=(400, 640))
    for slot_id, ratio in report.contrast.items():
        assert ratio >= _CONTRAST_THRESHOLDS[slot_id], (name, slot_id, ratio)
    assert not any("still" in w and "threshold" in w for w in report.warnings)
    assert report.adjustments == []
