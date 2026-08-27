"""The skin contract: what one cheap call turns a manuscript sample into.

This is the same object the party-assembly page renders — the model fills the
costume, the code owns everything mechanical (lanes, prices, party rules). The
schema is strict-mode friendly (every field required, no extras), so a reply
either validates or the caller falls back to DEFAULT_SKIN; a half-filled skin
never reaches the page.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CharacterSkin(BaseModel):
    """One party member's costume for this book: a name and a voice."""
    alias: str
    job: str


class SkinSpec(BaseModel):
    """The whole costume for one manuscript.

    `palette` is a closed set the page maps to CSS themes — the model picks the
    nearest mood, it does not invent colors. `maturity` is register only: it
    tunes how knowing the copy is allowed to be, never its explicitness.
    `is_fiction` and `language` are honesty flags — a memoir or a Portuguese
    novel still gets a skin, but the caller can see the stretch."""
    book_title: str
    genre: str                          # e.g. "spicy romantasy", "noir thriller"
    maturity: Literal["cozy", "standard", "mature"]
    palette: Literal["ember", "rose", "rain", "honey", "void", "neon",
                     "verdigris", "bone", "gold", "slate", "rust", "frost"]
    language: str
    is_fiction: bool
    themes: list[str]                   # a few words each, for logging/QA
    narration: str                      # Galley's greeting, grounded in the book
    empty_party: str                    # the empty party-zone line, in register
    empty_bench: str                    # the everyone-hired line, in register
    signoff: str                        # the how-we'll-reach-you joke
    pip: CharacterSkin
    bram: CharacterSkin
    maple: CharacterSkin
    cinder: CharacterSkin
    sage: CharacterSkin
    lark: CharacterSkin


# The party at home: base names, fantasy register. This is what every book gets
# when the model's answer does not come back usable — the page always renders.
DEFAULT_SKIN = SkinSpec(
    book_title="Your Manuscript",
    genre="epic fantasy",
    maturity="standard",
    palette="ember",
    language="English",
    is_fiction=True,
    themes=["adventure"],
    narration=("I read your first pages. Here's the party I'd bring — "
               "swap anyone you like, or slip them something from the shop."),
    empty_party="An empty campfire… drag an adventurer over.",
    empty_bench="Everyone's been hired!",
    signoff=("we'll send a raven (okay, an email) when the loot is ready."),
    pip=CharacterSkin(alias="Pip",
                      job="Hunts typos and misspelled words."),
    bram=CharacterSkin(alias="Bram",
                       job="Keeps grammar and punctuation lawful."),
    maple=CharacterSkin(alias="Maple",
                        job="Keeps names spelled the same on page 12 "
                            "and page 312."),
    cinder=CharacterSkin(alias="Cinder",
                         job="Reforges broken sentences. Tangled ones "
                             "become boss fights."),
    sage=CharacterSkin(alias="Sage",
                       job="Remembers if the queen's eyes changed color "
                           "between chapters."),
    lark=CharacterSkin(alias="Lark",
                       job="Suggests where a line could sing. Never "
                           "rewrites without asking."),
)
