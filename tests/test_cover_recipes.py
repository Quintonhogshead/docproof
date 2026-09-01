"""Finishing recipes (deep-stack wave, §15.6): the recipes.py registry, the
Direction/Archetype recipe plumbing, build_spec's expansion into real
fx_-prefixed spec layers, and the shipped seven-recipe roster — each of
which must expand valid and procedural-render through the autopilot and
balance pass (this file IS the "malformed shipped recipe fails its own unit
test loudly" backstop recipes.py's shallow loader leans on).

No network anywhere; canvases are tiny (400x640) — the same fractional-
geometry reasoning as every other cover test file.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from docproof.cover.archetypes import (ARCHETYPES, Archetype, ArchetypeArt,
                                       ArchetypeText, ArchetypeZone)
from docproof.cover.compose import _CONTRAST_THRESHOLDS, compose
from docproof.cover.model import (AdjustLayer, ArtSlot, Brief, CoverSpec,
                                  Direction, Palette, build_spec)
from docproof.cover.recipes import (RECIPES, RECIPES_DIR, RecipeError,
                                    describe_recipes, load_recipes)

CANVAS = (400, 640)

SHIPPED_RECIPES = ("airbrushed_glow", "cinematic_duotone", "dark_academia",
                   "midnight_neon", "pulp_print", "quiet_literary",
                   "vintage_matte")


def _palette(**overrides) -> Palette:
    data = dict(background="#101820", primary="#c9382c", accent="#c9a227",
               text="#f5f1e8", scrim="#000000")
    data.update(overrides)
    return Palette(**data)


def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", subtitle="A Novel",
               author="J. R. Vance", genre="literary")
    data.update(overrides)
    return Brief(**data)


def _direction(archetype: str = "probe_typographic", recipe: str = "") -> Direction:
    return Direction(concept_name="Test", rationale="test",
                     archetype=archetype, palette=_palette(),
                     title_font="Spectral", author_font="Spectral",
                     art_prompts=[], texture=True, recipe=recipe)


# ===========================================================================
# recipes.py: the registry and its shallow checks
# ===========================================================================

def test_registry_loads_exactly_the_shipped_roster():
    assert set(RECIPES) == set(SHIPPED_RECIPES)
    assert RECIPES_DIR.is_dir()
    assert {p.stem for p in RECIPES_DIR.glob("*.yaml")} == set(RECIPES)


@pytest.mark.parametrize("name", SHIPPED_RECIPES)
def test_every_shipped_recipe_has_the_documented_shape(name):
    recipe = RECIPES[name]
    assert recipe["name"] == name
    assert recipe["describe"].strip()
    finish = recipe["finish"]
    # §15.6's roster rule: 4-8 finish entries, each a one-key art/adjust
    # mapping, every id wearing the reserved fx_ prefix.
    assert 4 <= len(finish) <= 8
    for entry in finish:
        (kind, fields), = entry.items()
        assert kind in ("art", "adjust")
        assert fields["id"].startswith("fx_")


def test_describe_recipes_mentions_every_recipe_once():
    text = describe_recipes()
    for name, recipe in RECIPES.items():
        assert f"- {name} — " in text
        assert " ".join(recipe["describe"].split())[:40] in text
    assert text.count("\n") == len(RECIPES) - 1


def test_vintage_matte_is_the_specs_worked_example():
    # docs/cover_designer_spec.md §15.6's YAML, entry for entry (one
    # documented substitution: the spec's paper_tooth plate is not on the
    # shipped shelf — laid_paper stands in; see the recipe file's header).
    finish = RECIPES["vintage_matte"]["finish"]
    shapes = [(next(iter(e)), next(iter(e.values()))["id"]) for e in finish]
    assert shapes == [("adjust", "fx_lift"), ("adjust", "fx_warm"),
                      ("art", "fx_paper"), ("adjust", "fx_vign"),
                      ("art", "fx_grain")]
    assert finish[2]["art"]["texture_file"] == "laid_paper"
    assert finish[3]["adjust"] == {"id": "fx_vign", "op": "vignette",
                                   "strength": 0.28}


def test_load_recipes_missing_dir_is_an_empty_shelf(tmp_path):
    assert load_recipes(tmp_path / "nowhere") == {}


@pytest.mark.parametrize("body,complaint", [
    ("describe: x\nfinish: [{adjust: {id: fx_a}}]\n", "name"),
    ("name: broken\nfinish: [{adjust: {id: fx_a}}]\n", "describe"),
    ("name: broken\ndescribe: x\n", "finish"),
    ("name: broken\ndescribe: x\nfinish: []\n", "finish"),
    ("name: broken\ndescribe: x\nfinish: [{paint: {id: fx_a}}]\n", "finish[0]"),
    ("name: broken\ndescribe: x\nfinish: [{adjust: {id: fx_a}}]\nbonus: 1\n",
     "bonus"),
    ("name: wrong\ndescribe: x\nfinish: [{adjust: {id: fx_a}}]\n",
     "does not match"),
])
def test_shallow_checks_fail_loudly_and_name_the_file(tmp_path, body, complaint):
    path = tmp_path / "broken.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(RecipeError) as err:
        load_recipes(tmp_path)
    assert "broken.yaml" in str(err.value)
    assert complaint in str(err.value)


# ===========================================================================
# The plumbing: Direction.recipe / Archetype.recipe / build_spec expansion
# ===========================================================================

def test_direction_recipe_is_a_closed_literal_over_the_shelf():
    assert _direction(recipe="vintage_matte").recipe == "vintage_matte"
    assert _direction().recipe == ""
    with pytest.raises(ValueError):
        _direction(recipe="instagram_filter")


def test_archetype_rejects_a_recipe_that_is_not_on_the_shelf():
    with pytest.raises(ValueError, match="instagram_filter"):
        Archetype(
            name="synthetic", describe="test", composition_note="test",
            recipe="instagram_filter",
            art=[ArchetypeArt(id="background", generatable=False)],
            text=[ArchetypeText(id="title",
                                zone=ArchetypeZone(x=0.1, y=0.1, w=0.8, h=0.2),
                                size_min=0.04, size_max=0.08)],
            layers=["background", "title"])


def test_expansion_appends_real_layers_at_the_top_in_order():
    spec = build_spec(_direction(recipe="vintage_matte"), _brief(),
                      ARCHETYPES["probe_typographic"])
    tail = [(l.kind, l.ref) for l in spec.layers[-5:]]
    assert tail == [("adjust", "fx_lift"), ("adjust", "fx_warm"),
                    ("art", "fx_paper"), ("adjust", "fx_vign"),
                    ("art", "fx_grain")]
    # Real model instances on the spec itself — expansion, not indirection.
    art_by_id = {a.id: a for a in spec.art}
    assert isinstance(art_by_id["fx_paper"], ArtSlot)
    assert art_by_id["fx_paper"].texture_file == "laid_paper"
    adjust_by_id = {a.id: a for a in spec.adjust}
    assert isinstance(adjust_by_id["fx_vign"], AdjustLayer)
    assert adjust_by_id["fx_vign"].strength == 0.28
    # And the document revalidates as-is: fully self-contained.
    CoverSpec.model_validate(spec.model_dump())


def test_direction_pick_wins_over_the_archetype_default():
    # big_type's own default is quiet_literary (PR4 retrofit) — a direction
    # that names vintage_matte gets vintage_matte, nothing of the default.
    spec = build_spec(_direction(recipe="vintage_matte"), _brief(),
                      ARCHETYPES["probe_typographic"])
    fx_ids = {l.ref for l in spec.layers if l.ref.startswith("fx_")}
    assert "fx_lift" in fx_ids            # vintage_matte's
    assert "fx_hush" not in fx_ids        # quiet_literary's


def test_silent_direction_falls_back_to_the_archetype_default():
    spec = build_spec(_direction(), _brief(), ARCHETYPES["probe_typographic"])
    assert {a.id for a in spec.adjust} == {"fx_hush", "fx_warm", "fx_vign"}


def test_no_recipe_anywhere_expands_nothing():
    # An un-retrofitted archetype + a silent direction: zero fx_ layers,
    # zero adjust entries — the byte-identical no-recipe path's spec shape.
    spec = build_spec(_direction(archetype="probe_ornament"), _brief(),
                      ARCHETYPES["probe_ornament"])
    assert spec.adjust == []
    assert not any(l.ref.startswith("fx_") for l in spec.layers)


def test_recipe_layers_survive_patch_edits_as_ordinary_fields():
    # §6.2's guarantee, §15.6 edition: "halve fx_grain" is one ordinary
    # field edit on the dumped document — no re-expansion machinery.
    spec = build_spec(_direction(recipe="quiet_literary"), _brief(),
                      ARCHETYPES["probe_typographic"])
    dump = spec.model_dump()
    for slot in dump["art"]:
        if slot["id"] == "fx_grain":
            slot["opacity"] = 0.02
    edited = CoverSpec.model_validate(dump)
    assert next(a for a in edited.art if a.id == "fx_grain").opacity == 0.02


# ===========================================================================
# The roster proof: every shipped recipe renders green (§15.15 PR4)
# ===========================================================================

@pytest.mark.parametrize("name", SHIPPED_RECIPES)
def test_every_shipped_recipe_expands_and_renders_through_the_autopilot(name):
    spec = build_spec(_direction(recipe=name), _brief(), ARCHETYPES["probe_typographic"])
    image, report = compose(spec, Path("/nonexistent"), canvas=CANVAS)
    assert image.size == CANVAS
    # Autopilot: every rendered slot's final measured contrast clears its
    # own threshold, and neither the draw-time loop nor the §15.7 re-check
    # gave up ("still ... against threshold" is their shared surrender
    # wording).
    for slot_id, ratio in report.contrast.items():
        assert ratio >= _CONTRAST_THRESHOLDS[slot_id], (name, slot_id, ratio)
    assert not any("still" in w and "threshold" in w for w in report.warnings)
    # Balance pass ran (report-only for an axis-less spec: measurements
    # flow into warnings, the snap channel stays empty rather than absent).
    assert report.adjustments == []


def test_recipe_versus_no_recipe_changes_pixels_with_zero_image_spend():
    # Acceptance item (a), procedurally: same brief, recipe="" vs a real
    # grade — visibly different pixels, no generated assets involved.
    bare = build_spec(_direction(archetype="probe_ornament"), _brief(),
                      ARCHETYPES["probe_ornament"])
    graded_direction = _direction(archetype="probe_ornament",
                                  recipe="cinematic_duotone")
    graded = build_spec(graded_direction, _brief(), ARCHETYPES["probe_ornament"])
    img_bare, _ = compose(bare, Path("/nonexistent"), canvas=CANVAS)
    img_graded, _ = compose(graded, Path("/nonexistent"), canvas=CANVAS)
    assert img_bare.tobytes() != img_graded.tobytes()


# ===========================================================================
# Packaging: the shipped roster reaches the wheel
# ===========================================================================

def test_pyproject_ships_the_recipes_directory_as_package_data():
    # The CI-friendly half of the scratch-venv proof (the wheel itself is
    # built and inspected out-of-band): pyproject declares the package-data
    # entry, and the files it globs are really there.
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["tool"]["setuptools"]["package-data"]["config.cover.recipes"] \
        == ["*.yaml"]
    assert sorted(p.stem for p in RECIPES_DIR.glob("*.yaml")) == \
        sorted(SHIPPED_RECIPES)
