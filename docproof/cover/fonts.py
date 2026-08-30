"""Cover Studio's font shelf: the display faces the art-direction call picks
from and the composer renders with.

A static registry on purpose — no fontTools, no font discovery, no per-book
font upload. Every family here resolves to a vendored TTF the composer can
already load, so a Direction's title_font/author_font choice is
schema-enforced against exactly what exists: the model cannot pick a face
that is not on the shelf (see docproof.cover.model.Direction).

Two roots (§15.11 of docs/cover_designer_spec.md):

- config/prep/fonts — the ten launch families, shared with the book-interior
  design. They stay where they are and keep their exact model-visible names
  (those names are API: archived directions and revision transcripts refer
  to them).
- config/cover/fonts — the cover-owned expansion shelf, ~23 further families
  vendored from https://github.com/google/fonts (OFL/Apache only, fsType=0,
  static cuts only; see its README.md for the family → file → license map).

docproof is the library layer and must not import from `app` — resource_root()
lives in app/settings.py and is off limits here — so this resolves its own
config subtree the way docproof's own modules already do (docproof/genre.py's
_genres_dir(), docproof/consistency.py's _CONSISTENCY_DIR): walk up from this
file to the installed package root, where config/ ships as a sibling of
docproof/ (see pyproject.toml's [tool.setuptools.packages.find] and
package-data — both roots MUST stay listed under package-data or the wheel
build silently drops the TTFs and Fly FileNotFounds at render time).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# docproof/cover/fonts.py -> docproof/cover -> docproof -> package root, the
# same depth docproof/eval/candidate_eval.py's CANDIDATE_CASES walks.
FONTS_DIR = Path(__file__).resolve().parents[2] / "config" / "prep" / "fonts"
COVER_FONTS_DIR = (
    Path(__file__).resolve().parents[2] / "config" / "cover" / "fonts")

# Shelf-convention buckets (§15.11): describe_fonts() groups by these, and the
# direction prompt teaches the model to match the bucket to the genre's shelf
# (the §6.3 judge flags a script-titled thriller or a Bebas romance).
Role = Literal["display_serif", "didone", "slab", "sans", "condensed_caps",
               "script", "blackletter", "mono", "decorative", "small_caps"]


@dataclass(frozen=True)
class CoverFont:
    """One face on the shelf."""
    family: str          # display name, the value model calls choose
    file: str            # filename under this family's root directory
    vibe: str            # one line for the direction prompt
    caps_friendly: bool  # good tracked-uppercase face
    role: Role           # shelf-convention bucket, drives prompt grouping
    italic_file: str = ""  # style companion when the face ships one
    bold_file: str = ""
    # Suggested author-line partners (registered family names), fed to the
    # direction prompt as pairing hints — hints, not validation: the model may
    # pair anything with anything.
    pairs_with: tuple[str, ...] = field(default=())


# --- The launch ten (config/prep/fonts) --------------------------------------
# One static weight each — chosen for cover DISPLAY use, which is not always
# the weight the book-interior design (config/prep/book_design.yaml) picks
# from the same file set. Family names match config/prep/fonts/README.md's
# "Family name in Word" column exactly: TextSlot and Direction validate
# against these strings, and the composer has to find a file under the same
# name a human reading the spec would recognise. DO NOT rename — the names
# are model-visible API.
_PREP_FAMILIES: dict[str, CoverFont] = {
    "Spectral": CoverFont(
        family="Spectral", file="Spectral-SemiBold.ttf",
        vibe="A warm literary serif with old-style warmth — the house body "
             "face, with enough weight to carry a title or an author line.",
        caps_friendly=True, role="display_serif",
        italic_file="Spectral-Italic.ttf", bold_file="Spectral-Bold.ttf",
        pairs_with=("EB Garamond", "Lora")),
    "IM FELL English": CoverFont(
        family="IM FELL English", file="IMFellEnglish-Regular.ttf",
        vibe="An antique letterpress revival, inked and slightly irregular "
             "— reads like an 18th-century first edition.",
        caps_friendly=False, role="display_serif",
        pairs_with=("EB Garamond", "Spectral")),
    "EB Garamond": CoverFont(
        family="EB Garamond", file="EBGaramond-Regular.ttf",
        vibe="A classic Garamond revival — elegant, restrained, timeless; "
             "the safe choice when nothing louder is called for.",
        caps_friendly=True, role="display_serif",
        pairs_with=("Spectral", "Cormorant Garamond")),
    "Playfair Display": CoverFont(
        family="Playfair Display", file="PlayfairDisplay-Regular.ttf",
        vibe="A high-contrast editorial display serif — dramatic thick-thin "
             "strokes, upscale and a little theatrical.",
        caps_friendly=True, role="didone",
        pairs_with=("EB Garamond", "Poppins")),
    "Cormorant Garamond": CoverFont(
        family="Cormorant Garamond", file="CormorantGaramond-Medium.ttf",
        vibe="An elongated, high-contrast Garamond — delicate and romantic; "
             "wants generous tracking and room to breathe.",
        caps_friendly=True, role="display_serif",
        pairs_with=("EB Garamond", "Great Vibes")),
    "Lora": CoverFont(
        family="Lora", file="Lora-Regular.ttf",
        vibe="A contemporary serif with calligraphic roots — warm and "
             "steady, quietly contemporary rather than showy.",
        caps_friendly=False, role="display_serif",
        pairs_with=("Spectral", "Poppins")),
    "Quicksand": CoverFont(
        family="Quicksand", file="Quicksand-Medium.ttf",
        vibe="A rounded geometric sans — friendly and soft-edged, at home "
             "on YA and cozy covers.",
        caps_friendly=True, role="sans",
        pairs_with=("Poppins", "Spectral")),
    "Orbitron": CoverFont(
        family="Orbitron", file="Orbitron-Medium.ttf",
        vibe="A geometric sci-fi sans, angular and wide — built for tracked "
             "caps on a starship hull.",
        caps_friendly=True, role="sans",
        pairs_with=("Space Mono", "Poppins")),
    "Special Elite": CoverFont(
        family="Special Elite", file="SpecialElite-Regular.ttf",
        vibe="A distressed typewriter face — inked-ribbon texture, built "
             "for noir and epistolary conceits.",
        caps_friendly=False, role="mono",
        pairs_with=("Spectral", "Space Mono")),
    "Pirata One": CoverFont(
        family="Pirata One", file="PirataOne-Regular.ttf",
        vibe="A blackletter display face — gothic and heavy with ornament; "
             "horror and dark fantasy's default shout.",
        caps_friendly=False, role="blackletter",
        pairs_with=("IM FELL English", "Spectral")),
}

# --- The expansion shelf (config/cover/fonts, §15.11) ------------------------
# Vendored from https://github.com/google/fonts (raw files under ofl/<slug>/),
# every license verified OFL 1.1 and every file fsType=0 at vendoring time.
# Static cuts only; four spec candidates ship variable-only upstream and were
# substituted (Oswald → Staatliches, Dancing Script → Pacifico, Cinzel →
# Cinzel Decorative, Archivo → Poppins — noted in the directory README).
_COVER_FAMILIES: dict[str, CoverFont] = {
    # condensed caps — thriller / crime / big-type nonfiction
    "Bebas Neue": CoverFont(
        family="Bebas Neue", file="BebasNeue-Regular.ttf",
        vibe="The modern blockbuster condensed sans — tall, tight, "
             "all-business; the thriller aisle's default shout.",
        caps_friendly=True, role="condensed_caps",
        pairs_with=("Poppins", "Spectral")),
    "Anton": CoverFont(
        family="Anton", file="Anton-Regular.ttf",
        vibe="A heavy condensed grotesque with real ink weight — louder and "
             "squarer than Bebas Neue, built to fill a jacket edge to edge.",
        caps_friendly=True, role="condensed_caps",
        pairs_with=("Poppins", "Fjalla One")),
    "Staatliches": CoverFont(
        family="Staatliches", file="Staatliches-Regular.ttf",
        vibe="A squared-off condensed caps display with a public-signage "
             "flavor — punchy without the action-movie growl.",
        caps_friendly=True, role="condensed_caps",
        pairs_with=("Poppins", "Spectral")),
    "Archivo Black": CoverFont(
        family="Archivo Black", file="ArchivoBlack-Regular.ttf",
        vibe="An ultra-black grotesque — flat-footed, massive, unmissable; "
             "big-idea nonfiction and manifesto covers.",
        caps_friendly=True, role="condensed_caps",
        pairs_with=("Poppins", "Space Mono")),
    # didone / fat-face display serifs — prestige drama
    "Abril Fatface": CoverFont(
        family="Abril Fatface", file="AbrilFatface-Regular.ttf",
        vibe="A fat-face didone straight off a Victorian poster — enormous "
             "contrast, curvy and confident; loves mixed case at huge sizes.",
        caps_friendly=True, role="didone",
        pairs_with=("Poppins", "EB Garamond")),
    "Rozha One": CoverFont(
        family="Rozha One", file="RozhaOne-Regular.ttf",
        vibe="A high-contrast display didone with sharp ball terminals — "
             "editorial drama with a slightly exotic edge.",
        caps_friendly=True, role="didone",
        pairs_with=("Poppins", "Spectral")),
    "Yeseva One": CoverFont(
        family="Yeseva One", file="YesevaOne-Regular.ttf",
        vibe="A curvaceous display serif with swelling rounds — romantic "
             "and a little vintage, happiest in mixed case.",
        caps_friendly=False, role="didone",
        pairs_with=("EB Garamond", "Poppins")),
    "Libre Caslon Display": CoverFont(
        family="Libre Caslon Display", file="LibreCaslonDisplay-Regular.ttf",
        vibe="A Caslon tuned for headlines — bookish authority with "
             "old-world charm; the prestige literary title face.",
        caps_friendly=True, role="display_serif",
        pairs_with=("EB Garamond", "Spectral")),
    # slab — middle-grade / cozy / sturdy contemporary
    "Alfa Slab One": CoverFont(
        family="Alfa Slab One", file="AlfaSlabOne-Regular.ttf",
        vibe="A rounded ultra-bold slab — friendly billboard weight; "
             "middle-grade, cozy mystery, and anything that should feel fun.",
        caps_friendly=True, role="slab",
        pairs_with=("Poppins", "Quicksand")),
    "Zilla Slab": CoverFont(
        family="Zilla Slab", file="ZillaSlab-Regular.ttf",
        vibe="A contemporary medium slab — sturdy, quietly techy warmth "
             "that also holds up small on an author line.",
        caps_friendly=True, role="slab",
        italic_file="ZillaSlab-Italic.ttf", bold_file="ZillaSlab-Bold.ttf",
        pairs_with=("Poppins", "Spectral")),
    # script — romance / romcom / lifestyle
    "Great Vibes": CoverFont(
        family="Great Vibes", file="GreatVibes-Regular.ttf",
        vibe="A formal flowing calligraphic script with long connecting "
             "strokes — wedding-invitation elegance for romance covers.",
        caps_friendly=False, role="script",
        pairs_with=("EB Garamond", "Cormorant Garamond")),
    "Sacramento": CoverFont(
        family="Sacramento", file="Sacramento-Regular.ttf",
        vibe="A light monoline retro script — casual 1950s hand-lettering, "
             "breezier and quieter than Great Vibes.",
        caps_friendly=False, role="script",
        pairs_with=("Poppins", "EB Garamond")),
    "Pacifico": CoverFont(
        family="Pacifico", file="Pacifico-Regular.ttf",
        vibe="A thick brush script with surf-shop bounce — chunky enough to "
             "survive thumbnails; romcom and cozy energy.",
        caps_friendly=False, role="script",
        pairs_with=("Poppins", "Quicksand")),
    # engraved / small caps — historical / fantasy / classical prestige
    "Playfair Display SC": CoverFont(
        family="Playfair Display SC", file="PlayfairDisplaySC-Regular.ttf",
        vibe="Playfair's small-caps cut — engraved editorial elegance for "
             "author lines and understated titles.",
        caps_friendly=True, role="small_caps",
        pairs_with=("Playfair Display", "EB Garamond")),
    "Cinzel Decorative": CoverFont(
        family="Cinzel Decorative", file="CinzelDecorative-Regular.ttf",
        vibe="An engraved Trajan-style titling face with decorative "
             "flourishes — antiquity, epic fantasy, marble and gold.",
        caps_friendly=True, role="small_caps",
        pairs_with=("Marcellus", "EB Garamond")),
    "Marcellus": CoverFont(
        family="Marcellus", file="Marcellus-Regular.ttf",
        vibe="A Roman inscriptional serif, calm and classical — historical "
             "fiction's quiet authority.",
        caps_friendly=True, role="small_caps",
        pairs_with=("EB Garamond", "Cinzel Decorative")),
    "Julius Sans One": CoverFont(
        family="Julius Sans One", file="JuliusSansOne-Regular.ttf",
        vibe="A hairline engraved caps sans — airy, lapidary, luxurious "
             "with wide tracking.",
        caps_friendly=True, role="small_caps",
        pairs_with=("Marcellus", "Poppins")),
    # decorative one-shots — period and concept covers
    "Monoton": CoverFont(
        family="Monoton", file="Monoton-Regular.ttf",
        vibe="A five-line neon-tube display face — retro sci-fi marquee "
             "glow in a single word.",
        caps_friendly=True, role="decorative",
        pairs_with=("Space Mono", "Poppins")),
    "Rye": CoverFont(
        family="Rye", file="Rye-Regular.ttf",
        vibe="A slab-serifed western circus face, spurred and ornamented — "
             "wanted-poster Americana.",
        caps_friendly=True, role="decorative",
        pairs_with=("Special Elite", "Spectral")),
    "UnifrakturMaguntia": CoverFont(
        family="UnifrakturMaguntia", file="UnifrakturMaguntia-Book.ttf",
        vibe="A textura blackletter, denser and more medieval than Pirata "
             "One — gothic horror and dark history; never set it in all caps.",
        caps_friendly=False, role="blackletter",
        pairs_with=("IM FELL English", "EB Garamond")),
    # workhorse sans — contemporary nonfiction / support faces
    "Fjalla One": CoverFont(
        family="Fjalla One", file="FjallaOne-Regular.ttf",
        vibe="A sturdy medium-condensed sans — workmanlike display that "
             "stays out of the art's way.",
        caps_friendly=True, role="sans",
        pairs_with=("Poppins", "Spectral")),
    "Poppins": CoverFont(
        family="Poppins", file="Poppins-Regular.ttf",
        vibe="A clean geometric sans with perfectly round Os — the "
             "contemporary self-help and big-idea nonfiction voice.",
        caps_friendly=True, role="sans",
        bold_file="Poppins-Bold.ttf",
        pairs_with=("Space Mono", "Spectral")),
    # mono — sci-fi / experimental / documentary
    "Space Mono": CoverFont(
        family="Space Mono", file="SpaceMono-Regular.ttf",
        vibe="A retro-futuristic fixed-width face — NASA-transcript charm "
             "for sci-fi and experimental literary covers.",
        caps_friendly=True, role="mono",
        bold_file="SpaceMono-Bold.ttf",
        pairs_with=("Poppins", "Spectral")),
}

FAMILIES: dict[str, CoverFont] = {**_PREP_FAMILIES, **_COVER_FAMILIES}

# Which root each family's files live under. Kept out of CoverFont so the
# dataclass stays exactly the §15.11 shape (and so a family can never point
# outside the two vendored roots).
_ROOTS: dict[str, Path] = (
    {name: FONTS_DIR for name in _PREP_FAMILIES}
    | {name: COVER_FONTS_DIR for name in _COVER_FAMILIES})

# The author line's fallback family when a direction supplies none — the
# registry's one deliberately weightier text face (FAMILIES["Spectral"]
# resolves to the SemiBold cut, not Regular, for exactly this reason),
# legible small and tracked.
AUTHOR_FONT_DEFAULT = "Spectral"

# Prompt-facing gloss per role, in shelf-walk order — describe_fonts() renders
# one group per entry. Every Role literal member must appear here exactly once
# (tests enforce), so adding a bucket without a gloss fails loudly.
_ROLE_ORDER: tuple[tuple[Role, str], ...] = (
    ("condensed_caps", "poster-weight tracked caps; thriller, crime, "
                       "big-type nonfiction"),
    ("didone", "high-contrast display serifs; prestige drama, romance, "
               "editorial"),
    ("display_serif", "literary serif title and author faces; the bookish "
                      "default"),
    ("slab", "sturdy slab serifs; middle-grade, cozy, contemporary"),
    ("sans", "clean display sans; contemporary fiction and nonfiction"),
    ("small_caps", "engraved and small-caps faces; historical, fantasy, "
                   "classical prestige"),
    ("script", "hand-lettered scripts; romance, romcom, lifestyle — title "
               "or accent words only, never long lines"),
    ("blackletter", "gothic blackletter; horror, dark fantasy, dark "
                    "history — never tracked caps"),
    ("mono", "typewriter and terminal faces; noir, sci-fi, epistolary, "
             "documentary"),
    ("decorative", "loud period one-shots; use only when the concept IS "
                   "the typeface"),
)


def font_path(family: str, style: str = "regular") -> Path:
    """The TTF path for a registered family, optionally one of its style
    companions (style in {"regular", "italic", "bold"}).

    Raises KeyError — the same as a bare dict lookup — for an unregistered
    name: callers reach this after the model layer has already validated
    `family` against FAMILIES (TextSlot and Direction both do), so an unknown
    name here is a programming error, not user input worth a friendlier
    message. Raises ValueError for a style the family does not ship — callers
    wanting italic/bold must check CoverFont.italic_file/.bold_file first
    (the §15.12 emphasis validator does).
    """
    font = FAMILIES[family]
    if style == "regular":
        file = font.file
    elif style == "italic":
        file = font.italic_file
    elif style == "bold":
        file = font.bold_file
    else:
        raise ValueError(f"unknown font style {style!r} "
                         "(expected regular/italic/bold)")
    if not file:
        raise ValueError(f"{family} ships no {style} companion")
    return _ROOTS[family] / file


def describe_fonts() -> str:
    """The shelf as prompt text for the art-direction call
    (docproof.cover.direction) — grouped by role with a per-group gloss, one
    line per family carrying its vibe, case guidance, style companions, and
    suggested author-line pairings. The model picks title_font/author_font
    from this described, closed list (same shape as
    docproof.prep.book_design.BookDesign.describe_subjects())."""
    lines: list[str] = []
    for role, gloss in _ROLE_ORDER:
        lines.append(f"{role.replace('_', ' ').upper()} — {gloss}:")
        for name, font in FAMILIES.items():
            if font.role != role:
                continue
            notes = ["tracked caps" if font.caps_friendly else "mixed case"]
            companions = [s for s, f in (("italic", font.italic_file),
                                         ("bold", font.bold_file)) if f]
            if companions:
                notes.append(f"{' & '.join(companions)} available")
            if font.pairs_with:
                notes.append(
                    f"pairs with {', '.join(font.pairs_with)}")
            lines.append(f"- {name} — {font.vibe} ({'; '.join(notes)})")
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = ["AUTHOR_FONT_DEFAULT", "COVER_FONTS_DIR", "FAMILIES", "FONTS_DIR",
           "CoverFont", "Role", "describe_fonts", "font_path"]
