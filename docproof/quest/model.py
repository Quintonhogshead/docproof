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
    """One party member's costume for this book: a name, a voice, and what an
    illustrator would need to draw them in this book's world."""
    alias: str
    job: str
    look: str


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
    themes: list[str]
    narration: str
    empty_party: str
    empty_bench: str
    signoff: str
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
                      job="Hunts typos and misspelled words.",
                      look="Small and quick, a young scout in a travel-worn "
                           "cloak, bright eyes over a knowing grin."),
    bram=CharacterSkin(alias="Bram",
                       job="Keeps grammar and punctuation lawful.",
                       look="Broad and steady, a knight in plain polished "
                            "armor with a ledger on his belt."),
    maple=CharacterSkin(alias="Maple",
                        job="Keeps names spelled the same on page 12 "
                            "and page 312.",
                        look="Precise and ink-stained, spectacles on a chain, "
                             "a satchel of well-kept records."),
    cinder=CharacterSkin(alias="Cinder",
                         job="Reforges broken sentences. Tangled ones "
                             "become boss fights.",
                         look="Strong-armed at a small forge, leather apron, "
                              "sparks caught mid-air around her hammer."),
    sage=CharacterSkin(alias="Sage",
                       job="Remembers if the queen's eyes changed color "
                           "between chapters.",
                       look="Old and unhurried, a long coat full of "
                            "bookmarks, eyes that miss nothing."),
    lark=CharacterSkin(alias="Lark",
                       job="Suggests where a line could sing. Never "
                           "rewrites without asking.",
                       look="Bright-eyed with an instrument slung on their "
                            "back and a notebook always half open."),
)
