"""The §15.11 font-library expansion: both roots resolve, every registered
file (style companions included) actually loads in Pillow, roles are valid
Literal members, pairing hints resolve, and describe_fonts() presents the
shelf grouped by role — the closed list the direction model picks from.

Companion coverage for the launch shelf lives in test_cover_archetypes.py;
this file owns the expansion contract.
"""
from pathlib import Path
from typing import get_args

import pytest
from PIL import ImageFont

from docproof.cover.fonts import (AUTHOR_FONT_DEFAULT, COVER_FONTS_DIR,
                                  FAMILIES, FONTS_DIR, Role, describe_fonts,
                                  font_path)

# The ten launch families — model-visible names, frozen API (§15.11: "prep's
# 10 stay where they are and stay registered").
_LAUNCH_TEN = {
    "Spectral", "IM FELL English", "EB Garamond", "Playfair Display",
    "Cormorant Garamond", "Lora", "Quicksand", "Orbitron", "Special Elite",
    "Pirata One",
}


# -- roster shape -------------------------------------------------------------

def test_roster_grew_to_spec_size_and_kept_the_launch_ten():
    assert _LAUNCH_TEN <= set(FAMILIES)
    new = set(FAMILIES) - _LAUNCH_TEN
    assert 18 <= len(new) <= 25, sorted(new)


def test_every_role_bucket_is_stocked():
    stocked = {font.role for font in FAMILIES.values()}
    assert stocked == set(get_args(Role))


def test_every_family_has_a_valid_role():
    roles = set(get_args(Role))
    for name, font in FAMILIES.items():
        assert font.role in roles, f"{name}: bad role {font.role!r}"


def test_every_family_carries_its_own_name():
    for name, font in FAMILIES.items():
        assert font.family == name
        assert isinstance(font.vibe, str) and font.vibe.strip()


# -- files exist and load -----------------------------------------------------

def test_every_registered_file_loads_in_pillow():
    for name in FAMILIES:
        path = font_path(name)
        assert path.is_file(), f"{name}: no file at {path}"
        assert path.suffix == ".ttf"
        ImageFont.truetype(str(path), 14)  # raises on a broken file


def test_style_companions_exist_and_load():
    seen = 0
    for name, font in FAMILIES.items():
        for style, file in (("italic", font.italic_file),
                            ("bold", font.bold_file)):
            if not file:
                continue
            seen += 1
            path = font_path(name, style)
            assert path.is_file(), f"{name} {style}: no file at {path}"
            ImageFont.truetype(str(path), 14)
    assert seen >= 4  # Spectral + Zilla Slab ship both; Poppins/Space Mono bold


def test_font_path_rejects_a_missing_companion_and_a_bad_style():
    # Monoton is a single-cut display face — no italic ships.
    with pytest.raises(ValueError, match="no italic companion"):
        font_path("Monoton", "italic")
    with pytest.raises(ValueError, match="unknown font style"):
        font_path("Spectral", "wavy")
    with pytest.raises(KeyError):
        font_path("Comic Sans")


# -- the two roots ------------------------------------------------------------

def test_launch_families_still_resolve_to_prep_paths():
    for name in _LAUNCH_TEN:
        assert font_path(name).parent == FONTS_DIR, name


def test_expansion_families_resolve_to_the_cover_root():
    for name in set(FAMILIES) - _LAUNCH_TEN:
        assert font_path(name).parent == COVER_FONTS_DIR, name


def test_cover_root_has_no_orphan_ttfs_and_ships_licenses():
    registered = set()
    for name, font in FAMILIES.items():
        if font_path(name).parent != COVER_FONTS_DIR:
            continue
        registered.update(
            f for f in (font.file, font.italic_file, font.bold_file) if f)
    on_disk = {p.name for p in COVER_FONTS_DIR.glob("*.ttf")}
    assert on_disk == registered
    # Vendoring hygiene: license texts ship alongside, and the provenance
    # README names every TTF and every license file.
    licenses = sorted(p.name for p in COVER_FONTS_DIR.glob("*-OFL.txt"))
    assert licenses, "no license texts vendored"
    readme = (COVER_FONTS_DIR / "README.md").read_text()
    for filename in sorted(on_disk) + licenses:
        assert filename in readme, f"README.md misses {filename}"


def test_expansion_shelf_stays_inside_the_size_budget():
    total = sum(p.stat().st_size for p in COVER_FONTS_DIR.iterdir())
    assert total < 6 * 1024 * 1024, f"{total} bytes vendored"


# -- pairing hints ------------------------------------------------------------

def test_pairs_with_names_resolve_to_registered_families():
    for name, font in FAMILIES.items():
        for partner in font.pairs_with:
            assert partner in FAMILIES, f"{name} pairs with unknown {partner}"
            assert partner != name, f"{name} pairs with itself"


def test_author_font_default_is_registered():
    assert AUTHOR_FONT_DEFAULT in FAMILIES


# -- describe_fonts: the prompt roster ----------------------------------------

def _role_heading(role: str) -> str:
    return role.replace("_", " ").upper() + " — "


def test_describe_fonts_groups_every_family_under_its_role():
    text = describe_fonts()
    # Slice the text into role sections in rendered order.
    positions = {}
    for role in get_args(Role):
        heading = _role_heading(role)
        assert heading in text, f"missing role group {role}"
        positions[role] = text.index(heading)
    ordered = sorted(positions.items(), key=lambda kv: kv[1])
    for i, (role, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
        section = text[start:end]
        for name, font in FAMILIES.items():
            if font.role == role:
                assert f"- {name} — " in section, \
                    f"{name} not grouped under {role}"


def test_describe_fonts_carries_vibes_and_pairing_hints():
    text = describe_fonts()
    for name, font in FAMILIES.items():
        assert font.vibe in text, f"{name}: vibe missing"
        if font.pairs_with:
            assert f"pairs with {', '.join(font.pairs_with)}" in text, \
                f"{name}: pairing hint missing"
