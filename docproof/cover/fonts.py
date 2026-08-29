"""Cover Studio's font shelf: ten display faces the art-direction call picks
from and the composer renders with.

A small, static registry on purpose — no fontTools, no font discovery, no
per-book font upload. Every family here is one the composer can already load
(the same TTFs config/prep/fonts ships for the book-interior design), so a
Direction's title_font/author_font choice is schema-enforced against exactly
what exists: the model cannot pick a face that is not on the shelf (see
docproof.cover.model.Direction).

docproof is the library layer and must not import from `app` — resource_root()
lives in app/settings.py and is off limits here — so this resolves its own
config subtree the way docproof's own modules already do (docproof/genre.py's
_genres_dir(), docproof/consistency.py's _CONSISTENCY_DIR): walk up from this
file to the installed package root, where config/ ships as a sibling of
docproof/ (see pyproject.toml's [tool.setuptools.packages.find] and
package-data).

Expansion of the font library is out of scope for v1; wiring in more families
is the obvious later win once this shelf has been used on real briefs.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# docproof/cover/fonts.py -> docproof/cover -> docproof -> package root, the
# same depth docproof/eval/candidate_eval.py's CANDIDATE_CASES walks.
FONTS_DIR = Path(__file__).resolve().parents[2] / "config" / "prep" / "fonts"


@dataclass(frozen=True)
class CoverFont:
    """One face on the shelf."""
    family: str          # display name, the value model calls choose
    file: str            # filename under config/prep/fonts
    vibe: str             # one line for the direction prompt
    caps_friendly: bool  # good tracked-uppercase face


# Ten faces, one static weight each — chosen for cover DISPLAY use, which is
# not always the weight the book-interior design (config/prep/book_design.yaml)
# picks from the same file set. Family names match config/prep/fonts/README.md's
# "Family name in Word" column exactly: TextSlot and Direction validate against
# these strings, and the composer has to find a file under the same name a
# human reading the spec would recognise.
FAMILIES: dict[str, CoverFont] = {
    "Spectral": CoverFont(
        family="Spectral", file="Spectral-SemiBold.ttf",
        vibe="A warm literary serif with old-style warmth — the house body "
             "face, with enough weight to carry a title or an author line.",
        caps_friendly=True),
    "IM FELL English": CoverFont(
        family="IM FELL English", file="IMFellEnglish-Regular.ttf",
        vibe="An antique letterpress revival, inked and slightly irregular "
             "— reads like an 18th-century first edition.",
        caps_friendly=False),
    "EB Garamond": CoverFont(
        family="EB Garamond", file="EBGaramond-Regular.ttf",
        vibe="A classic Garamond revival — elegant, restrained, timeless; "
             "the safe choice when nothing louder is called for.",
        caps_friendly=True),
    "Playfair Display": CoverFont(
        family="Playfair Display", file="PlayfairDisplay-Regular.ttf",
        vibe="A high-contrast editorial display serif — dramatic thick-thin "
             "strokes, upscale and a little theatrical.",
        caps_friendly=True),
    "Cormorant Garamond": CoverFont(
        family="Cormorant Garamond", file="CormorantGaramond-Medium.ttf",
        vibe="An elongated, high-contrast Garamond — delicate and romantic; "
             "wants generous tracking and room to breathe.",
        caps_friendly=True),
    "Lora": CoverFont(
        family="Lora", file="Lora-Regular.ttf",
        vibe="A contemporary serif with calligraphic roots — warm and "
             "steady, quietly contemporary rather than showy.",
        caps_friendly=False),
    "Quicksand": CoverFont(
        family="Quicksand", file="Quicksand-Medium.ttf",
        vibe="A rounded geometric sans — friendly and soft-edged, at home "
             "on YA and cozy covers.",
        caps_friendly=True),
    "Orbitron": CoverFont(
        family="Orbitron", file="Orbitron-Medium.ttf",
        vibe="A geometric sci-fi sans, angular and wide — built for tracked "
             "caps on a starship hull.",
        caps_friendly=True),
    "Special Elite": CoverFont(
        family="Special Elite", file="SpecialElite-Regular.ttf",
        vibe="A distressed typewriter face — inked-ribbon texture, built "
             "for noir and epistolary conceits.",
        caps_friendly=False),
    "Pirata One": CoverFont(
        family="Pirata One", file="PirataOne-Regular.ttf",
        vibe="A blackletter display face — gothic and heavy with ornament; "
             "horror and dark fantasy's default shout.",
        caps_friendly=False),
}

# The author line's fallback family when a direction supplies none — the
# registry's one deliberately weightier text face (FAMILIES["Spectral"]
# resolves to the SemiBold cut, not Regular, for exactly this reason),
# legible small and tracked.
AUTHOR_FONT_DEFAULT = "Spectral"


def font_path(family: str) -> Path:
    """The TTF path for a registered family. Raises KeyError — the same as a
    bare dict lookup — for an unregistered name: callers reach this after the
    model layer has already validated `family` against FAMILIES (TextSlot and
    Direction both do), so an unknown name here is a programming error, not
    user input worth a friendlier message."""
    return FONTS_DIR / FAMILIES[family].file


def describe_fonts() -> str:
    """The shelf as prompt text, one line per family — the same shape as
    docproof.prep.book_design.BookDesign.describe_subjects() — for the
    art-direction call (docproof.cover.direction) to pick a title_font/
    author_font from a described, closed list."""
    lines = []
    for name, font in FAMILIES.items():
        tag = "tracked caps" if font.caps_friendly else "mixed case"
        lines.append(f"- {name} — {font.vibe} ({tag})")
    return "\n".join(lines)


__all__ = ["AUTHOR_FONT_DEFAULT", "FAMILIES", "FONTS_DIR", "CoverFont",
          "describe_fonts", "font_path"]
