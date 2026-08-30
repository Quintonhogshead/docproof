"""Cover Studio: an AI-assisted ebook cover designer.

A CoverSpec is the unit of work — a JSON document describing everything about
a cover except the raster art pixels. An art-direction model call turns a
Brief into several Directions; build_spec merges one Direction into its
chosen archetype to produce a CoverSpec; a deterministic composer renders it
with real embedded fonts. See docs/cover_designer_spec.md for the whole
design, and docproof/quest/skin.py for the sibling feature this one's model-
call conventions are modeled on.
"""
from __future__ import annotations

from .archetypes import (ARCHETYPES, Archetype, ArchetypeError,
                         describe_archetypes, zone_px)
from .fonts import AUTHOR_FONT_DEFAULT, FAMILIES, CoverFont, describe_fonts, font_path
from .model import (ArtSlot, Brief, ConceptState, CoverSpec, Direction,
                    Directions, JobState, LayerRef, Palette, PaletteRole,
                    RenderReport, ScrimSpec, Shadow, Stroke, TextSlot,
                    build_spec)

__all__ = [
    # model.py
    "ArtSlot", "Brief", "ConceptState", "CoverSpec", "Direction",
    "Directions", "JobState", "LayerRef", "Palette", "PaletteRole",
    "RenderReport", "ScrimSpec", "Shadow", "Stroke", "TextSlot", "build_spec",
    # fonts.py
    "AUTHOR_FONT_DEFAULT", "FAMILIES", "CoverFont", "describe_fonts",
    "font_path",
    # archetypes.py
    "ARCHETYPES", "Archetype", "ArchetypeError", "describe_archetypes",
    "zone_px",
]

# docproof.cover.direction / imaging / typeset / compose / pipeline are NOT
# imported here, on purpose, and never should be. Each of those modules owns
# its own public names; import them directly —
#   from docproof.cover.compose import compose
#   from docproof.cover.pipeline import run_job
# — rather than adding to this file. This file re-exports model.py, fonts.py,
# and archetypes.py only, and stays that way; each later module's author
# extends their OWN module's exports, never this one.
