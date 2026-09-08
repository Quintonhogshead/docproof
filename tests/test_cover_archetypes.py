"""docproof/cover/archetypes.py (loading, zone math, layer resolution) and
docproof/cover/fonts.py (the font registry) — the two static-resource
foundation modules underneath docproof/cover/model.py.

No network. The "malformed file fails loudly" tests write real, broken YAML
to tmp_path and load it through the same code path config/cover/archetypes/
loads through at import — see docs/cover_designer_spec.md §5.1 and §11.
"""
from __future__ import annotations

import pathlib

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
from docproof.cover.textures import TEXTURES
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
    # One "- name — describe" ENTRY line per archetype. Not one line per
    # archetype: a template that declares `casting` rides extra indented lines
    # under its own entry, so the entry count is the invariant, not the line
    # count.
    entries = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(entries) == len(ARCHETYPES)


# -- casting: the doctrine that reaches the art-direction call ---------------
#
# A template whose slots are ROLES rather than nouns has to tell the director
# how to fill them, or the director fills them from the reference cover it was
# shown instead of from the book it was given. `casting` is the channel; these
# guard that it actually reaches the enumeration and that silent templates are
# unaffected.

def test_casting_text_reaches_describe_archetypes():
    casting = ARCHETYPES["elemental_aperture"].casting
    assert casting, "elemental_aperture is a role-slot template; it must cast"
    text = describe_archetypes()
    for slot in ("aperture", "beyond", "herald", "field", "drift"):
        assert f"{slot} —" in text, slot


def test_casting_is_indented_under_its_own_archetype():
    casting_by_name = {a.name: a.casting for a in ARCHETYPES.values()}
    owner = None
    for line in describe_archetypes().splitlines():
        if line.startswith("- "):
            owner = line.split(" — ")[0].removeprefix("- ")
        elif line.startswith("    "):
            # An indented line only ever belongs to the entry above it, and
            # only a template that declares `casting` may emit one.
            assert casting_by_name.get(owner), (owner, line[:40])


def test_a_template_with_no_casting_adds_no_lines():
    # Byte-identical default path: every template that predates the field
    # still describes as exactly one line and contributes no indented ones.
    silent = [a for a in ARCHETYPES.values() if not a.casting]
    assert silent, "the no-casting path needs at least one template to guard"
    lines = describe_archetypes().splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("- "):
            continue
        name = line.split(" — ")[0].removeprefix("- ")
        if any(a.name == name for a in silent):
            following = lines[i + 1:i + 2]
            assert not following or following[0].startswith("- ")


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
    "romantasy_vignette": ["fantasy", "romance"],
    "gilded_descent": ["fantasy", "romance", "horror", "historical"],
    "pale_reliquary": ["fantasy", "romance"],
    "crossed_relics": ["fantasy", "romance", "mystery_thriller",
                              "science_fiction", "historical"],
    "romantasy_enclosure": ["fantasy", "romance"],
    "portrait_luminary": ["fantasy", "romance"],
    "elemental_aperture": ["fantasy", "science_fiction", "romance",
                          "mystery_thriller", "horror"],
    "gilded_sigil": ["fantasy", "science_fiction", "romance",
                    "mystery_thriller", "young_readers"],
    "uplit_vigil": ["fantasy", "romance", "horror"],
    "gilded_cartouche": ["fantasy", "romance", "historical", "horror"],
    "burning_cartouche": ["fantasy", "romance", "horror"],
    "sable_regalia": ["fantasy", "horror"],
}


def test_new_archetypes_have_the_expected_genres():
    assert set(_EXPECTED_GENRES) == set(SHIPPED_ARCHETYPES)
    for name, genres in _EXPECTED_GENRES.items():
        assert ARCHETYPES[name].genres == genres


def test_describe_archetypes_emits_each_slot_role():
    """A slot id is a label, not a brief. `role` is the slot's own statement
    of what it is FOR, and until it was emitted here it reached no prompt
    anywhere — leaving the art-direction call to guess what "luminary" or
    "token_near" meant and fill it from the nearest cover it could remember
    instead of from the book. Guard both halves: the exact id still appears
    (a misspelled slot is silently dropped downstream), and the role rides
    with it."""
    text = describe_archetypes()
    for archetype in ARCHETYPES.values():
        for slot in archetype.art:
            if not slot.generatable:
                continue
            assert slot.id in text, f"{archetype.name}: {slot.id} not offered"
            if slot.role:
                assert f"{slot.id} — {' '.join(slot.role.split())}" in text, (
                    f"{archetype.name}: {slot.id}'s role never reaches the "
                    f"direction prompt")


def test_portrait_luminary_carries_no_prop_slot():
    """The template asks questions about the book, not for a shot list. A
    `relic` slot (a vertical object up the left third — a sword on the cover
    this template was drawn from) was the one slot that was a NOUN rather
    than a question, and it invited every book onto the same shelf of
    weapons. Anything the subject carries belongs in `hero`, on them."""
    ids = {slot.id for slot in ARCHETYPES["portrait_luminary"].art}
    assert "relic" not in ids


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
    # Count ENTRY lines ("- name — describe"), not raw lines: an archetype's
    # optional `casting` block is emitted as an indented paragraph underneath
    # its entry, so a raw line count measures how chatty the matching
    # templates are rather than how many of them matched.
    def _entries(text: str) -> int:
        return sum(1 for line in text.splitlines() if line.startswith("- "))

    assert _entries(describe_archetypes("historical")) < len(ARCHETYPES)


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


# -- photoreal templates (§19.4) ----------------------------------------------

def test_photoreal_archetype_is_marked_for_the_director():
    """direction.py bans untreated photorealism across the whole shelf. The
    exemption is worthless if the art-direction call cannot tell which
    templates hold it: told only "never ship an untreated photoreal prompt",
    a model picking one either refuses its own plates or reaches for
    photo_soft — which is a duotone, and destroys a multi-plate photographic
    template. The mark has to be at the point of choice."""
    text = describe_archetypes()
    for name, archetype in ARCHETYPES.items():
        marked = "[PHOTOREAL TEMPLATE" in _archetype_line(text, name)
        assert marked == archetype.photoreal, (
            f"{name}: photoreal={archetype.photoreal} but marked={marked}")


def _archetype_line(text: str, name: str) -> str:
    for line in text.split("\n"):
        if line.startswith(f"- {name} "):
            return line
    raise AssertionError(f"{name} not described")


def test_photoreal_requires_a_finishing_recipe():
    """Half of what the flag ASSERTS: the grade, the bloom and above all the
    grain are what put separately generated plates on one piece of film.
    Without them the exemption is permission to ship untreated stock."""
    base = dict(
        name="probe_photoreal", describe="d", composition_note="c",
        art=[{"id": "a", "generatable": True}],
        text=[{"id": "title", "zone": {"x": 0, "y": 0, "w": 1, "h": 1},
               "size_min": 0.01, "size_max": 0.1}],
        layers=["a", "title"], photoreal=True)
    with pytest.raises(ValidationError):
        Archetype(**base)
    assert Archetype(**base, recipe="cinematic_duotone").photoreal


def test_photoreal_defaults_off_so_the_shelf_rule_still_binds():
    """Every archetype that does not opt in stays under the shelf-wide ban —
    the exemption is opt-in, never a silent widening."""
    assert Archetype(
        name="probe_default", describe="d", composition_note="c",
        art=[{"id": "a", "generatable": True}],
        text=[{"id": "title", "zone": {"x": 0, "y": 0, "w": 1, "h": 1},
               "size_min": 0.01, "size_max": 0.1}],
        layers=["a", "title"]).photoreal is False
    assert ARCHETYPES["romantasy_organic"].photoreal is False


def test_the_photoreal_exemption_reaches_the_direction_prompt():
    """The mark in the archetype list means nothing unless the rule the call
    reads actually carves the exemption out — the two halves ship together
    or the call is told to obey a rule it has been marked exempt from."""
    from docproof.cover import direction as direction_module
    source = pathlib.Path(direction_module.__file__).read_text(encoding="utf-8")
    assert "THE ONE EXEMPTION" in source
    assert source.count("[PHOTOREAL TEMPLATE]") >= 3


# -- place_by (ink vs frame) --------------------------------------------------

def test_place_by_defaults_to_frame_everywhere_it_is_not_declared():
    # The whole point of the field being opt-in: every slot written before it
    # existed keeps the plate-frame placement it was tuned against.
    for archetype in ARCHETYPES.values():
        for slot in archetype.art:
            if slot.place_by == "ink":
                assert slot.fit == "contain", (
                    f"{archetype.name}.{slot.id}: ink placement is meaningless "
                    f"on a cover fit, which fills the canvas either way")


def test_pale_reliquary_places_every_contain_slot_by_its_ink():
    # Archetype Six's rule 0. A contain-fit slot here is anchored on a severed
    # edge, and the frame measurement cannot honour that: it flushes the
    # plate's transparent margin to the trim instead of the subject's cut.
    a = ARCHETYPES["pale_reliquary"]
    contain = [s for s in a.art if s.fit == "contain"]
    assert contain
    assert all(s.place_by == "ink" for s in contain), [
        s.id for s in contain if s.place_by != "ink"]


def test_a_keep_whole_slot_declares_no_cut_edge():
    # The two are contradictory instructions: keep_whole says this subject has
    # no severed end to carry out through the trim, cut_edge says which end to
    # carry out. A slot asserting both is an authoring mistake.
    for archetype in ARCHETYPES.values():
        for slot in archetype.art:
            if slot.keep_whole:
                assert not slot.cut_edge, (
                    f"{archetype.name}.{slot.id}: keep_whole and "
                    f"cut_edge={slot.cut_edge!r} contradict each other")


def test_pale_reliquary_keeps_its_discrete_object_plates_whole():
    a = ARCHETYPES["pale_reliquary"]
    by_id = {s.id: s for s in a.art}
    for sid in ("berries_l", "berries_r"):
        assert by_id[sid].keep_whole, f"{sid} is a scatter of spheres"
    # ...and the plates that DO have a severed end still overshoot.
    for sid in ("crown", "undergrowth", "beast", "bloom", "claw"):
        assert not by_id[sid].keep_whole
        assert by_id[sid].cut_edge


# -- Archetype Nine (gilded_sigil) — the six rules, as regression guards -----
#
# Every assertion below is a rule the template's own header states and which a
# well-meaning later edit would plausibly "fix" in the wrong direction. They
# are written against gilded_sigil by name (not swept over the shelf) because
# each is a property of THIS arrangement, not of templates in general.

def test_gilded_sigil_snaps_its_heart_into_the_titles_own_line_gap():
    """Rule 5. `snap: line_gap` only fires for a CONTAIN-fit slot drawn
    IMMEDIATELY after a text layer (compose._position_all_art reads
    layers[i - 1]); slide one layer between them and the disc silently
    reverts to its fixed anchor, which is the wrong place for every book.
    And snap measures ALPHA, so the plate must carry real transparency —
    rule 4's one exception."""
    a = ARCHETYPES["gilded_sigil"]
    heart = next(s for s in a.art if s.id == "heart")
    assert heart.snap == "line_gap"
    assert heart.fit == "contain"
    assert heart.transparent, "snap and the occlusion guards measure alpha"
    assert heart.blend == "normal", "a screened plate has no alpha to snap by"
    i = a.layers.index("heart")
    assert a.layers[i - 1] == "title"


def test_gilded_sigil_leaves_its_foot_type_unprotected_on_purpose():
    """Rule 2, and the single most likely wrong 'fix' in this file. The pale
    foot band plus near-black author ink is produced by compose's two-ink
    flip, which only runs once scrim escalation has nothing left to
    escalate. Give `author` or `series` a scrim and the autopilot darkens
    the band instead of flipping the ink, and rule 1's value inversion is
    gone."""
    a = ARCHETYPES["gilded_sigil"]
    protected = {s.protects for s in a.scrims}
    assert "author" not in protected
    assert "series" not in protected
    # ...while the two slots that sit on the DARK four-fifths keep theirs.
    assert protected == {"title", "subtitle"}


def test_gilded_sigil_builds_its_pale_foot_deterministically():
    """Rule 1. The band may not depend on how the generated `drift` plate
    came back, so it is a color_wash in the `text` role behind a feathered
    gradient mask — drawn after the vignette (which would otherwise dirty
    the one bright band on the cover) and before the two text slots that
    stand on it."""
    a = ARCHETYPES["gilded_sigil"]
    foot = next(j for j in a.adjust if j.id == "foot_plate")
    assert foot.op == "color_wash"
    assert foot.color == "text"
    assert foot.mask is not None and foot.mask.gradient is not None
    assert foot.mask.gradient.angle == 90.0
    order = a.layers.index
    assert order("edge_fall") < order("foot_plate") < order("series")
    assert order("foot_plate") < order("author")


def test_gilded_sigil_quarantines_its_one_hue_by_mask():
    """Rule 3. The metallic is APPLIED, not hoped for: one gradient_map per
    struck plate, each onto background -> accent -> text and each masked to
    its own plate. The mask SOURCE differs by plate kind and that is the
    load-bearing detail — an opaque screened plate has solid alpha, so a
    from_layer stencil would tint the whole canvas; its luminance is the
    linework."""
    a = ARCHETYPES["gilded_sigil"]
    by_id = {j.id: j for j in a.adjust}
    art_by_id = {s.id: s for s in a.art}
    for adj_id, plate in (("sigil_ink", "sigil"),
                          ("outrider_ink", "outrider"),
                          ("heart_ink", "heart")):
        adj = by_id[adj_id]
        assert adj.op == "gradient_map"
        # The near plates get the third stop — a white specular, which is what
        # says NEAR. `outrider` is the same kind of thing seen far off and
        # deliberately ends at `accent`: given a specular it renders as a
        # bright swoosh in the disc's own plane (aerial perspective).
        assert adj.stops == (["background", "accent"] if plate == "outrider"
                             else ["background", "accent", "text"])
        assert adj.mask is not None
        if art_by_id[plate].transparent:
            assert adj.mask.from_layer == plate
        else:
            assert adj.mask.luminance_of == plate, (
                f"{plate} is opaque — a from_layer stencil tints everything")
        assert a.layers.index(adj_id) > a.layers.index(plate)


def test_gilded_sigil_screens_its_linework_instead_of_cutting_it_out():
    """Rule 4. A generator asked for a transparent PNG of thin symmetrical
    engraving returns a soft grey halo where the lines should be. Every
    plate but `heart` is prompted on pure black and screened."""
    a = ARCHETYPES["gilded_sigil"]
    for sid in ("sigil", "outrider", "drift"):
        slot = next(s for s in a.art if s.id == sid)
        assert slot.blend == "screen"
        assert not slot.transparent
        assert "on pure black" in " ".join(slot.prompt_frame.split()).lower()


def test_gilded_sigil_keeps_its_veil_undemotable():
    """The occlusion budget. `drift` is the only art drawn after the title
    that is not snapped, and it is COVER fit so the contain-fit sandwich
    machinery never looks at it — it cannot be demoted below the type. What
    keeps it honest instead is its own gradient mask, which holds it clear
    of the upper frame and opens toward the foot."""
    a = ARCHETYPES["gilded_sigil"]
    drift = next(s for s in a.art if s.id == "drift")
    assert drift.fit == "cover"
    assert a.layers.index("drift") > a.layers.index("title")
    assert drift.mask is not None and drift.mask.gradient is not None
    assert drift.mask.gradient.start > 0.0


def test_gilded_sigil_sets_the_author_in_the_title_face():
    """The deliberate opposite of romantasy_vignette's choice, and what this
    shelf actually does: on a brand-name hardcover the author's name is a
    second title, because the name is the thing being sold."""
    a = ARCHETYPES["gilded_sigil"]
    author = next(t for t in a.text if t.id == "author")
    assert author.font_role == "title"
    assert ARCHETYPES["romantasy_vignette"].text[-1].font_role == ""


def test_gilded_sigil_wears_no_shelf_recipe():
    """Rule 6. Every recipe that suits this genre runs a gradient_map onto
    background -> primary — the one ramp that does not contain the accent
    this whole template spends its chroma budget on (crossed_relics rule 5,
    learned again)."""
    assert ARCHETYPES["gilded_sigil"].recipe == ""
# -- uplit_vigil (archetype ten) ------------------------------------------
#
# Two rules here fight each other and the resolution is the whole design, so
# both halves are guarded. Rule 2 puts the figure on the TOP layer, which
# forbids the usual way of making a standing figure look grounded (paint some
# ground in front of her feet). Rule 3 still demands she look grounded. What
# is left is a hem that dissolves, a shadow pool she stands in, and a kerb of
# ground just behind her — plus rule 9's blur, which is the only depth cue the
# composition has left once nothing may overlap her.

def test_uplit_vigil_draws_its_figure_above_every_art_layer():
    """Rule 2. The figure is composited after every art plate and after the
    title; only the author's name — type, not paint — crosses her."""
    a = ARCHETYPES["uplit_vigil"]
    order = a.layers
    here = order.index("subject")
    for slot in a.art:
        if slot.id == "subject":
            continue
        assert order.index(slot.id) < here, f"{slot.id} is painted over the figure"
    assert here > order.index("title")
    assert here < order.index("author")


def test_uplit_vigil_dissolves_its_figures_hem_rather_than_cutting_it():
    """Rule 3, mechanism one — and with rule 2 having retired the occlusion,
    this is now the load-bearing one. Without the mask her plate ends on a hard
    horizontal cut line partway up the hazard band."""
    subject = next(s for s in ARCHETYPES["uplit_vigil"].art if s.id == "subject")
    assert subject.cut_edge == "bottom"
    assert subject.mask is not None and subject.mask.gradient is not None
    # angle 270 = bottom-transparent, top-opaque: the fade runs the right way.
    assert subject.mask.gradient.angle == 270.0
    # place_by ink (§15.24) is what lands her painted feet on the measured
    # point instead of her plate's transparent margin.
    assert subject.place_by == "ink"


def test_uplit_vigil_pools_its_contact_shadow_on_the_axis_before_the_figure():
    """Rule 3, mechanism two. The pool is defined by the template's centre
    axis rather than by the figure's silhouette, and that is FORCED, not
    chosen: a `from_layer` mask reads already-composited pixels, so rule 2's
    top-layer figure cannot be a mask source for anything. Guard both the
    ordering and the fact that no layer tries to mask off her."""
    a = ARCHETYPES["uplit_vigil"]
    pool = next(x for x in a.adjust if x.id == "foot_shadow")
    assert pool.mask is not None and pool.mask.gradient is not None
    assert pool.mask.gradient.kind == "radial"
    assert pool.mask.invert is True
    assert a.layers.index("foot_shadow") < a.layers.index("subject")
    # The pool sits under the axis the figure is anchored to.
    subject = next(s for s in a.art if s.id == "subject")
    assert a.axis == "center"
    assert pool.mask.gradient.center[0] == subject.anchor[0] == 0.5
    # Nothing may name the figure as a mask source — the loader refuses it,
    # and this asserts the template never tries.
    for layer in list(a.adjust) + list(a.art):
        mask = getattr(layer, "mask", None)
        if mask is not None:
            assert mask.from_layer != "subject"
            assert mask.luminance_of != "subject"


def test_uplit_vigil_keeps_a_kerb_of_ground_behind_the_figures_feet():
    """Rule 3, mechanism three. `hazard_near` no longer crosses in front of
    her, but it must still be drawn after the distant floor and off the centre
    axis, or she has no ground plane to meet at all."""
    a = ARCHETYPES["uplit_vigil"]
    order = a.layers
    assert order.index("hazard") < order.index("hazard_near") < order.index("subject")
    near = next(s for s in a.art if s.id == "hazard_near")
    assert near.anchor[0] != 0.5, "a plinth on the axis is not ground"
    # §15.24, and not optional: the prompt empties the plate's upper three
    # quarters, so frame-anchoring parks the whole strip below the trim.
    assert near.place_by == "ink"


def test_uplit_vigil_softens_the_background_and_nothing_else():
    """Rule 9: the blur is the LAST background operation. Every art plate but
    the figure is below it; every text layer and the figure are above it.
    Move it up and it smears the title; move it down and plates escape it."""
    a = ARCHETYPES["uplit_vigil"]
    order = a.layers
    blur = next(x for x in a.adjust if x.id == "back_soften")
    assert blur.op == "blur" and 0.0 < blur.opacity < 1.0
    here = order.index("back_soften")
    for slot in a.art:
        if slot.id == "subject":
            assert order.index(slot.id) > here
        else:
            assert order.index(slot.id) < here, f"{slot.id} escapes the softening"
    for slot in a.text:
        assert order.index(slot.id) > here, f"{slot.id} would be blurred"


def test_uplit_vigil_spends_its_occlusion_budget_to_zero():
    """Rule 2's price, asserted as the rule rather than as a slot list: no art
    plate is drawn after any text slot, so there is no sandwich left for the
    composer to silently demote."""
    a = ARCHETYPES["uplit_vigil"]
    order = a.layers
    last_art_below_type = max(
        order.index(s.id) for s in a.art if s.id != "subject")
    first_type = min(order.index(t.id) for t in a.text)
    assert last_art_below_type < first_type


def test_uplit_vigil_throws_the_floors_colour_back_onto_the_void():
    """Rule 1: `ember_wash` is the layer that makes sky and floor one
    photograph. It must screen `primary` from a centre below the bottom trim,
    inverted — a non-inverted radial mask lights the CORNERS instead."""
    a = ARCHETYPES["uplit_vigil"]
    wash = next(x for x in a.adjust if x.id == "ember_wash")
    assert wash.op == "color_wash" and wash.blend == "screen"
    assert wash.color == "primary"
    assert wash.mask is not None and wash.mask.gradient is not None
    assert wash.mask.gradient.kind == "radial"
    assert wash.mask.gradient.center[1] > 1.0
    assert wash.mask.invert is True
    # Between the floor and the figure, so she is rimmed by it, not washed over.
    assert a.layers.index("hazard") < a.layers.index("ember_wash") < a.layers.index("subject")


def test_uplit_vigil_sets_its_author_in_the_display_face():
    """Rule 7: the author's name is the second title. `font_role: title` is
    the only thing that puts it in the display face, and dropping it silently
    demotes the name to the eyebrow's supporting font."""
    author = next(t for t in ARCHETYPES["uplit_vigil"].text if t.id == "author")
    assert author.font_role == "title"
    title = next(t for t in ARCHETYPES["uplit_vigil"].text if t.id == "title")
    # near-title scale, not a caption
    assert author.size_max >= title.size_max * 0.9


def test_uplit_vigil_keeps_its_tagline_clear_of_the_figure():
    """Rule 8: the tagline fills the dead left band. Under rule 2 this is no
    longer only a taste rule — a tagline set wide enough to reach the figure
    gets her DEMOTED below it, which silently cancels rule 2. The zone must
    end well left of the centre axis, not merely start left of it."""
    tagline = next(t for t in ARCHETYPES["uplit_vigil"].text if t.id == "subtitle")
    assert tagline.align == "left"
    assert tagline.zone.x + tagline.zone.w <= 0.32


def test_uplit_vigil_leaves_its_frame_open_at_the_bottom():
    """Rule 4: three vapor plates, all entering from the top or the upper
    sides. A fourth closing the bottom makes this romantasy_vignette's wreath."""
    a = ARCHETYPES["uplit_vigil"]
    vapor = [s for s in a.art if s.id.startswith("vapor_")]
    assert len(vapor) == 3
    assert {s.cut_edge for s in vapor} == {"top", "left", "right"}
    # Asymmetry clause: the two side masses differ in height AND in scale.
    left = next(s for s in vapor if s.id == "vapor_left")
    right = next(s for s in vapor if s.id == "vapor_right")
    assert left.anchor[1] != right.anchor[1]
    assert left.scale != right.scale


def test_uplit_vigil_clamps_only_its_whole_fragment_plate():
    """Rule 6 and its one exception (§15.25): severed ends overshoot the trim,
    but the scatter of discrete whole pieces is clamped inside it."""
    a = ARCHETYPES["uplit_vigil"]
    whole = [s.id for s in a.art if s.keep_whole]
    assert whole == ["ember_drift"]
    for slot in a.art:
        if slot.cut_edge:
            assert not slot.keep_whole, f"{slot.id} both severs and clamps"


def test_uplit_vigil_dictates_no_pose():
    """Rule 10. The template used to hard-code "from BEHIND", "turned away"
    and "one arm is raised" into the figure's frame, which is one book's
    staging masquerading as structure. What it may still dictate is geometry
    and light; what it may not is which way the character faces."""
    subject = next(s for s in ARCHETYPES["uplit_vigil"].art if s.id == "subject")
    frame = subject.prompt_frame.lower()
    for banned in ("turned away", "from behind", "arm is raised", "raised arm",
                   "over one shoulder", "her back"):
        assert banned not in frame, f"the frame still dictates a pose: {banned!r}"
    # ...but the structure it DOES need is still spelled out.
    assert "full-length" in frame
    assert "one large simple pale shape" in frame
    assert "below" in frame            # the light direction, rule 1


# -- Archetype Twelve (burning_cartouche): the two laws §24 paid for -----------

def test_burning_cartouche_places_every_contain_slot_by_its_ink():
    # Same reasoning as pale_reliquary's rule 0 (§15.25): every contain-fit
    # slot here is anchored on a severed edge or clamped whole, and the frame
    # measurement can honour neither — it flushes the model's arbitrary
    # transparent margin to the trim instead of the subject.
    a = ARCHETYPES["burning_cartouche"]


# -- Archetype Thirteen: the single-saturation rule (§15.31) ------------------

def test_sable_regalia_mono_line_is_a_total_desaturation():
    """Rule 1. The whole template is a stacking order, and this is the layer
    the order is about: at anything softer than -1.0 a red-lit generation
    stays faintly red under the type, and the foil stops reading as foil."""
    a = ARCHETYPES["sable_regalia"]
    mono = next(adj for adj in a.adjust if adj.id == "mono_line")
    assert mono.op == "grade"
    assert mono.saturation == -1.0
    # ...and it is a FULL-FRAME grade: a mask would let a plate through.
    assert mono.mask is None
    assert mono.opacity == 1.0


def test_sable_regalia_every_mount_plate_is_below_the_mono_line():
    """The rule stated as an ordering assertion rather than as prose. Ground,
    ornament, glow, far tier, both arms and the crest are monochrome by
    construction; the two chroma plates and all four text slots are not."""
    a = ARCHETYPES["sable_regalia"]
    cut = a.layers.index("mono_line")
    below = set(a.layers[:cut])
    above = set(a.layers[cut + 1:])
    for slot in ("field", "damask", "halo", "fan", "crown_left",
                 "crown_right", "crest"):
        assert slot in below, f"{slot} must be desaturated by mono_line"
    for slot in ("seeds_back", "seeds_front", "brambles",
                 "title", "subtitle", "author", "series"):
        assert slot in above, f"{slot} carries chroma and must clear mono_line"


def test_sable_regalia_chroma_is_one_object_seen_at_two_depths():
    """`seeds_back` and `seeds_front` are the cover's entire colour budget
    besides the type, and the casting note tells the director to give them the
    same noun — so they must be the only two non-mount generatable plates
    above the line, and one of them must be in front of the title."""
    a = ARCHETYPES["sable_regalia"]
    assert a.layers.index("seeds_front") > a.layers.index("title")
    assert a.layers.index("seeds_back") < a.layers.index("title")


def test_sable_regalia_mounted_masses_dissolve_their_feet():
    """Rule 2, the §15.23 exception. Nothing here stands on anything, so the
    two plates that would otherwise end in a flat hem inside the frame carry
    an INVERTED linear gradient mask instead of a cut edge."""
    a = ARCHETYPES["sable_regalia"]
    by_id = {s.id: s for s in a.art}
    for sid in ("crest", "fan"):
        slot = by_id[sid]
        assert slot.mask is not None and slot.mask.gradient is not None, sid
        assert slot.mask.invert, f"{sid}'s foot fade must run opaque->clear"
        assert slot.mask.gradient.kind == "linear"
        assert not slot.cut_edge, (
            f"{sid} dissolves its foot; a cut edge is the other answer")


def test_sable_regalia_places_every_contain_slot_by_its_ink():
    # Archetype Six's rule 0 (§15.25), inherited: a contain-fit slot here is
    # anchored on either a severed edge or the centre axis, and the frame
    # measurement can honour neither.
    a = ARCHETYPES["sable_regalia"]
    contain = [s for s in a.art if s.fit == "contain"]
    assert contain
    assert all(s.place_by == "ink" for s in contain), [
        s.id for s in contain if s.place_by != "ink"]


def test_burning_cartouche_border_is_two_trim_pinned_columns_not_a_ring():
    """§24.1. The border was ONE cover-fit plate asked for "a ring with a large
    empty hole through the centre"; the generator filled the hole twice, the
    second time while honouring every other clause in the frame, and buried the
    field, the floor, the chain and the title's ground under one reef texture.

    A plate pinned to a side trim cannot fill the middle whatever comes back,
    so the hole is geometry now instead of a request. Guard the geometry: two
    columns, opposite trims, both cut on the edge they are anchored to."""
    by_id = {s.id: s for s in ARCHETYPES["burning_cartouche"].art}
    assert "bower" not in by_id, "the full-frame ring is the bug, not the design"
    left, right = by_id["bower_left"], by_id["bower_right"]
    assert (left.cut_edge, left.anchor[0]) == ("left", 0.0)
    assert (right.cut_edge, right.anchor[0]) == ("right", 1.0)
    # ...and the surplus width leaves through the trim (§24.2's visible-width
    # law): the offset pushes OUT, it does not pull the column inboard.
    assert left.offset[0] < 0 and right.offset[0] > 0
    # Rule 1: the organic half is asymmetric BY KIND as well as by placement.
    assert left.scale != right.scale
    assert left.anchor[1] != right.anchor[1]


def test_burning_cartouche_metal_is_symmetric_and_the_organic_is_not():
    """Rule 1, the whole design. `filigree` is one ornament kaleidoscoped into
    four byte-identical corners; nothing organic may wear `corners`."""
    by_id = {s.id: s for s in ARCHETYPES["burning_cartouche"].art}
    assert by_id["filigree"].corners
    assert by_id["filigree"].corners_flip_vertical, (
        "a rocaille scroll is top/bottom symmetric and wants the full mirror")
    for sid in ("bower_left", "bower_right", "bough", "floor"):
        assert not by_id[sid].corners, f"{sid} is the irregular half of rule 1"


def test_burning_cartouche_blaze_is_the_whole_occlusion_budget():
    """Rule 6. Exactly one plate is drawn after the title, it is on `screen`
    (which can only ADD light, so it glows over the byline instead of eating
    it), and §24.1's gradient mask fades its top out whatever shape the
    generator returns."""
    a = ARCHETYPES["burning_cartouche"]
    after_title = a.layers[a.layers.index("title") + 1:]
    art_ids = {s.id for s in a.art}
    crossing = [ref for ref in after_title if ref in art_ids]
    assert crossing == ["blaze"], crossing
    blaze = {s.id: s for s in a.art}["blaze"]
    assert blaze.blend == "screen"
    assert blaze.mask is not None and blaze.mask.gradient is not None


def test_burning_cartouche_hides_the_chains_cut_behind_the_relic():
    """§24.3. `pendant`'s severed end leaves through no trim — the relic's own
    body covers it — which only works if the chain is drawn FIRST. This
    ordering looks wrong in the layers list and is right on the page, so it
    gets a guard rather than a comment."""
    layers = ARCHETYPES["burning_cartouche"].layers
    assert layers.index("pendant") < layers.index("relic")


def test_burning_cartouche_puts_nothing_but_the_relic_on_the_altar():
    """Rule 3. An earlier draft carried a `strewn` slot — loose fragments of
    the border material come to rest around the relic's foot — and it was cut
    on owner note, for the same reason romantasy_enclosure's cabinet of
    curiosities and crossed_relics' second scatter were cut: made of the same
    stuff as the border, the fragments read as the border leaking into the
    middle, which is the one place this template defends. Every tuning it
    needed was a rule invented to stop it doing damage.

    The floor between the relic and the type is meant to be EMPTY, so guard
    the emptiness rather than trusting the comment."""
    a = ARCHETYPES["burning_cartouche"]
    assert "strewn" not in {s.id for s in a.art}
    # What is left on the centre axis below the border tier: the chain and the
    # relic, and nothing else.
    between = a.layers[a.layers.index("finial") + 1:a.layers.index("scrim:3")]
    assert between == ["pendant", "relic"], between
    # The relic rests rather than grows, hangs or spans, so it may not be
    # trim-cut (§15.25).
    assert {s.id: s for s in a.art}["relic"].keep_whole


def test_burning_cartouche_grounds_its_type_before_drawing_it():
    """§24.4. `foot_wash` sat AFTER the title for four tunings. That was
    harmless while its ramp started below the type and became the whole problem
    the moment the ramp was moved up to darken the title's ground: a wash over
    the type is a VEIL, and it dims the ink in exact proportion to how much it
    was supposed to be helping. The render read as a title fading out down a
    ramp — an ink fault it was not, so three successive tunings of the ink moved
    it essentially not at all. Fixing the order alone took title contrast from
    4.58 to 15.02.

    A wash is a ground only if it is drawn first."""
    a = ARCHETYPES["burning_cartouche"]
    wash = a.layers.index("foot_wash")
    for slot in ("subtitle", "title", "author"):
        assert wash < a.layers.index(slot), (
            f"foot_wash is drawn after {slot!r}, which veils it rather than "
            f"grounding it")
    # `series` is the exception and deliberately so: it lives in the crest's
    # medallion at the very top, nowhere near this wash's ramp.
    assert wash > a.layers.index("series")


def test_burning_cartouche_title_is_a_three_line_stack_in_a_tall_zone():
    """The uniform fit sizes every line to whatever the LONGEST line can carry,
    so the extra break is what buys the size: three lines throttled by
    "DROWNING" instead of two throttled by "DROWNING BELL", +27% on the fitted
    size. The tall zone is half of that decision — without the height the fit
    just re-throttles on the zone instead of the measure."""
    title = {t.id: t for t in ARCHETYPES["burning_cartouche"].text}["title"]
    assert title.max_lines >= 3
    assert title.zone.h >= 0.20
    # justify_stack was tried and is the trap here (§15.24's known gap): it
    # shrinks the block to the zone height and clamps short lines at size_max,
    # which returned a two-line title at 0.047 — smaller than the uniform fit's
    # 0.063 — under a giant "THE".
    assert title.fit_mode == "uniform"


def test_burning_cartouche_relic_is_loud_by_light_not_by_edge():
    """Rule 3, restated. Against a border of hundreds of small crisp objects a
    hard-edged, hard-lit focal plate is just one more hard thing; the relic
    wins by being the soft, lit, simple mass. The prompt says so and
    `relic_soften` backs it up in the engine, so the read does not depend on
    one generation coming back tender."""
    a = ARCHETYPES["burning_cartouche"]
    soften = {j.id: j for j in a.adjust}["relic_soften"]
    assert soften.op == "blur"
    assert soften.mask is not None and soften.mask.from_layer == "relic"
    assert a.layers.index("relic") < a.layers.index("relic_soften")
    frame = {s.id: s for s in a.art}["relic"].prompt_frame
    assert "soft" in frame.lower() and "no hard specular" in frame.lower()


def test_sable_regalia_discrete_objects_stay_whole_and_arms_overshoot():
    a = ARCHETYPES["sable_regalia"]
    by_id = {s.id: s for s in a.art}
    for sid in ("crest", "seeds_front"):
        assert by_id[sid].keep_whole, f"{sid} is one or more whole objects"
    for sid in ("crown_left", "crown_right", "brambles"):
        assert not by_id[sid].keep_whole
        assert by_id[sid].cut_edge, f"{sid} has a severed end to carry off"


def test_sable_regalia_arms_are_a_near_symmetry_not_a_mirror():
    """Rule 3. Two identical halves are a logo; the eye must read balance
    while the measurement reads difference."""
    a = ARCHETYPES["sable_regalia"]
    by_id = {s.id: s for s in a.art}
    left, right = by_id["crown_left"], by_id["crown_right"]
    assert a.axis == "center"
    assert left.anchor[1] != right.anchor[1]
    assert left.scale != right.scale
    assert left.prompt_frame != right.prompt_frame


def test_sable_regalia_title_is_a_built_foil_stamp():
    """Rule 5: flat ink is not foil. The metallic ramp and the bevel are the
    two effects that cannot be dropped without the type going to plain ink,
    and the ramp stops short of opaque so the autopilot's flip keeps a say."""
    a = ARCHETYPES["sable_regalia"]
    title = next(t for t in a.text if t.id == "title")
    kinds = [e.kind for e in title.effects]
    assert "gradient_overlay" in kinds and "bevel" in kinds
    ramp = next(e for e in title.effects if e.kind == "gradient_overlay")
    assert ramp.stops == ["accent", "primary"], "foil body -> foil highlight"
    assert ramp.opacity < 1.0
    # Rule 6: the justified stack is the emphasis, so a short last line runs
    # enormous — unreachable under the uniform fit.
    assert title.fit_mode == "justify_stack"


def test_sable_regalia_author_wears_the_display_face_and_the_same_foil():
    """The one shelf convention `font_role` was added for: the author's name
    is a second wordmark, not a credit block."""
    a = ARCHETYPES["sable_regalia"]
    author = next(t for t in a.text if t.id == "author")
    title = next(t for t in a.text if t.id == "title")
    assert author.font_role == "title"
    a_ramp = next(e for e in author.effects if e.kind == "gradient_overlay")
    t_ramp = next(e for e in title.effects if e.kind == "gradient_overlay")
    assert a_ramp.stops == t_ramp.stops
    assert a_ramp.opacity < t_ramp.opacity, "visibly the quieter stamp"


def test_sable_regalia_keeps_its_occlusion_budget_to_two_plates():
    """Half romantasy_vignette's, on purpose: this title is the largest
    object on the cover and its job is to be a wordmark."""
    a = ARCHETYPES["sable_regalia"]
    art_ids = {s.id for s in a.art}
    after_title = [n for n in a.layers[a.layers.index("title") + 1:]
                   if n in art_ids]
    assert after_title == ["brambles", "seeds_front"]
    for sid in after_title:
        slot = next(s for s in a.art if s.id == sid)
        assert any(e.kind == "drop_shadow" for e in slot.effects), (
            f"{sid} crosses the type; without a shadow onto the letterforms "
            f"it reads as a sticker")


def test_sable_regalia_all_over_ornament_degrades_to_a_shelf_plate():
    """Rule 4: the unbroken patterned field is the cheapest layer on the
    cover and the one it can least afford to lose, so it carries a $0
    fallback and lifts out of the black by screening rather than by paint."""
    a = ARCHETYPES["sable_regalia"]
    damask = next(s for s in a.art if s.id == "damask")
    assert damask.fit == "cover" and damask.blend == "screen"
    assert damask.texture_file in TEXTURES
    assert damask.opacity < 0.4, "tone-on-tone, not a foreground pattern"
