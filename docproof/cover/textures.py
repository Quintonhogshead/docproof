"""Cover Studio's texture shelf: stocked plates any ArtSlot may name via
`texture_file`, applied with the slot's own blend/opacity like any other art
layer (v2.2 wave, deliverable 5) — see config/cover/textures/README.md for
exactly which render each shelf plate started life as.

A small, static-at-import registry on purpose, the same shape
docproof.cover.fonts's FAMILIES already is — and it lives in its own module
rather than inside compose.py for the same reason: docproof.cover.model
needs to validate an ArtSlot's texture_file against this shelf AT SPEC-BUILD
TIME (the same field_validator pattern TextSlot.font_family already uses
against fonts.FAMILIES), and model.py must never import compose.py (see
model.py's own module docstring — the dependency only ever runs the other
way, compose.py imports FROM model.py). Putting the registry here, at the
same foundation layer as fonts.py, lets both model.py (validation) and
compose.py (the actual tile/cover-fit pixel work) import it without either
one reaching for the other.
"""
from __future__ import annotations

from pathlib import Path

# docproof/cover/textures.py -> docproof/cover -> docproof -> package root,
# the same depth docproof.cover.fonts.FONTS_DIR and
# docproof.cover.archetypes.ARCHETYPES_DIR both walk.
TEXTURES_DIR = Path(__file__).resolve().parents[2] / "config" / "cover" / "textures"

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


def load_textures(textures_dir: str | Path | None = None) -> dict[str, Path]:
    """Every stocked plate under `textures_dir` (package-relative by
    default), keyed by filename stem — `textures_dir` exists mainly so a
    test can point this at a tmp_path fixture, the same pattern
    docproof.cover.archetypes.load_archetypes's own `archetypes_dir`
    parameter follows.

    An empty or missing directory is not an error here (unlike
    load_archetypes, which insists the three launch archetypes are core
    inventory that must exist) — the texture shelf is additive polish, not
    load-bearing infrastructure, and CoverSpec/Archetype validation is what
    actually enforces that a NAMED texture_file exists on whatever shelf is
    loaded; a build with zero stocked plates should still import and render
    everything else fine."""
    root = Path(textures_dir) if textures_dir else TEXTURES_DIR
    if not root.is_dir():
        return {}
    return {p.stem: p for p in sorted(root.iterdir())
            if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES}


# Loaded once, at import — ArtSlot.texture_file/ArchetypeArt.texture_file
# both validate against this dict's keys (see docproof.cover.model.ArtSlot's
# own field_validator and its mirror in docproof.cover.archetypes), and
# compose.py's texture-rendering path reads the SAME dict for the actual
# file path, so the two can never disagree about what is "on the shelf."
TEXTURES: dict[str, Path] = load_textures()


__all__ = ["TEXTURES", "TEXTURES_DIR", "load_textures"]
