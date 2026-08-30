"""Cover Studio's recipe shelf: named, researched finishing stacks any
Direction (or archetype default) may pick by one word, expanded into real
spec layers at build_spec time (deep-stack wave, §15.6) — see
config/cover/recipes/README-less YAML files themselves for what each stack
is grounded in (every `describe` cites its docs/cover_template_research.md
convention).

A small, static-at-import registry on purpose, the same shape
docproof.cover.textures.TEXTURES already is — and it lives in its own module
rather than inside model.py for the same layering reason: model.py needs the
recipe NAMES at import time (the closed `Direction.recipe` Literal, built
via the same create_model trick the font roster uses) and the recipe BODIES
at build_spec time, while this module must stay importable with no cover
imports at all, or model.py↔recipes.py would cycle. Deep validation is
deliberately NOT done here: each finish entry is instantiated through the
real ArtSlot/AdjustLayer models inside build_spec, so a malformed shipped
recipe fails its own unit test loudly (tests/test_cover_recipes.py renders
every one) rather than needing a second, drifting copy of those models'
rules at this layer. What IS checked here is shape — name/describe/finish
present, finish a list of single-key art/adjust mappings — because a file
that shallow-broken would otherwise surface as a bewildering pydantic error
three modules away.
"""
from __future__ import annotations

from pathlib import Path

import yaml

# docproof/cover/recipes.py -> docproof/cover -> docproof -> package root,
# the same depth docproof.cover.textures.TEXTURES_DIR and
# docproof.cover.archetypes.ARCHETYPES_DIR both walk.
RECIPES_DIR = Path(__file__).resolve().parents[2] / "config" / "cover" / "recipes"


class RecipeError(Exception):
    """A recipe file that cannot be used — unreadable YAML or a shape the
    shallow checks reject. The message always names the file, because
    whoever hits this is editing a YAML stack, not Python (the
    archetypes.ArchetypeError convention)."""


def _shallow_check(path: Path, raw: object) -> dict:
    """The shape checks this layer owns (see the module docstring for why
    deep validation deliberately lives in build_spec instead): a mapping
    with a non-empty name matching the file stem, a describe line, and a
    finish list whose every entry is a one-key {art: {...}} or
    {adjust: {...}} mapping. Everything INSIDE those inner mappings is the
    real models' business."""
    if not isinstance(raw, dict):
        raise RecipeError(
            f"{path}: root must be a YAML mapping, not {type(raw).__name__}")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise RecipeError(f"{path}: missing or empty 'name'")
    if name != path.stem:
        raise RecipeError(
            f"{path}: name {name!r} does not match the file name "
            f"{path.stem!r}")
    if not isinstance(raw.get("describe"), str) or not raw["describe"].strip():
        raise RecipeError(f"{path}: missing or empty 'describe'")
    finish = raw.get("finish")
    if not isinstance(finish, list) or not finish:
        raise RecipeError(f"{path}: 'finish' must be a non-empty list")
    for i, entry in enumerate(finish):
        if (not isinstance(entry, dict) or len(entry) != 1
                or next(iter(entry)) not in ("art", "adjust")
                or not isinstance(next(iter(entry.values())), dict)):
            raise RecipeError(
                f"{path}: finish[{i}] must be exactly one of "
                f"{{art: {{...}}}} or {{adjust: {{...}}}}")
    extra = set(raw) - {"name", "describe", "finish"}
    if extra:
        raise RecipeError(
            f"{path}: unknown top-level key(s) {sorted(extra)!r} — a recipe "
            f"is exactly name/describe/finish")
    return raw


def load_recipes(recipes_dir: str | Path | None = None) -> dict[str, dict]:
    """Every recipe under `recipes_dir` (package-relative by default),
    keyed by name — `recipes_dir` exists mainly so a test can point this at
    a tmp_path fixture, the load_textures/load_archetypes pattern.

    A missing directory yields an empty shelf rather than an error (the
    textures posture: a build genuinely shipped without the roster should
    still import and render every recipe-less cover fine — and an archetype
    that DOES name a default recipe then fails its own load check loudly,
    which is the right place for that signal). A present-but-malformed FILE
    raises: a broken shipped recipe is a broken build, not a missing
    nicety."""
    root = Path(recipes_dir) if recipes_dir else RECIPES_DIR
    if not root.is_dir():
        return {}
    out: dict[str, dict] = {}
    for path in sorted(root.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise RecipeError(f"{path}: not readable YAML: {e}") from e
        out[path.stem] = _shallow_check(path, raw)
    return out


# Loaded once, at import — Direction.recipe's closed Literal and
# Archetype.recipe's load-time check both key off this dict, and
# model.build_spec reads the SAME dict for the actual finish entries, so
# the three can never disagree about what is on the shelf.
RECIPES: dict[str, dict] = load_recipes()


def describe_recipes() -> str:
    """The recipes as prompt text — name + describe line each, the exact
    shape describe_archetypes() already hands the art-direction call (the
    §15.14 vocabulary lands in a later PR; the function ships with the
    registry so that PR is a prompt edit, not a plumbing one)."""
    return "\n".join(f"- {name} — {' '.join(entry['describe'].split())}"
                     for name, entry in sorted(RECIPES.items()))


__all__ = ["RECIPES", "RECIPES_DIR", "RecipeError", "describe_recipes",
           "load_recipes"]
