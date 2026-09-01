"""Cover Studio probe archetypes: fixture templates chosen for their SHAPE.

Engine tests must not depend on what happens to be on the shipped archetype
shelf. They used to: ~290 assertions reached for ARCHETYPES["big_type"] and
friends, so retiring a template broke a third of the suite in files that had
nothing to do with templates.

tests/fixtures/cover_archetypes/ holds a small set of `probe_*` templates —
no-focal, sandwich, full-bleed, effects-rack, type-stack, glyph-mask,
gradient-seam. They are merged into the live ARCHETYPES registry at
collection time (see conftest) so the many call sites that resolve an
archetype BY NAME through the global dict keep working unchanged.

The shipped shelf stays visible alongside them. tests/test_cover_archetypes.py
is the one file that asserts shipped CONTENT, and it subtracts PROBE_ARCHETYPES
wherever it reasons about "the shelf".
"""
from __future__ import annotations

from pathlib import Path

from docproof.cover import archetypes as _cover_archetypes

PROBE_ARCHETYPE_DIR = Path(__file__).parent / "fixtures" / "cover_archetypes"
PROBE_ARCHETYPES: frozenset[str] = frozenset(
    p.stem for p in PROBE_ARCHETYPE_DIR.glob("*.yaml"))


def register_probe_archetypes() -> None:
    """Merge the probes into the live registry. Mutated IN PLACE, never
    rebound: pipeline.py, direction.py and the route modules all did
    `from .archetypes import ARCHETYPES` at import time, so they hold a
    reference to this exact dict object — rebinding the module attribute
    would leave every one of them looking at the old one.

    Idempotent, so importing this from both conftest and a test module is
    harmless."""
    loaded = _cover_archetypes.load_archetypes(PROBE_ARCHETYPE_DIR)
    clash = sorted(set(loaded) & set(_cover_archetypes.ARCHETYPES)
                   - set(PROBE_ARCHETYPES))
    if clash:
        raise RuntimeError(
            f"probe archetype name(s) {clash} collide with a shipped "
            f"archetype — rename the fixture, never the shipped template")
    _cover_archetypes.ARCHETYPES.update(loaded)
