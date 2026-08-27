"""The skin generator: one cheap, fast call that costumes the party for a book.

Drop a manuscript in, read a sample (opening plus a slice from the middle, so a
slow prologue doesn't get the only vote), and ask a small model to fill the
SkinSpec: aliases, job lines, Galley's greeting, a palette. Everything
mechanical — lanes, prices, party rules — stays in code; the model only ever
writes costume. A reply that doesn't validate falls back to DEFAULT_SKIN, so
the page renders for every upload, including the one where the model has a bad
day.

The call is deliberately tiny: ~6k tokens of sample through Luna is a fraction
of a cent, cheap enough to run at drop time before anyone has paid anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..promo.ingest import Manuscript, read_manuscript
from ..providers import Provider, cost_of_usage, strict_json_schema
from ..models import Usage
from .model import DEFAULT_SKIN, SkinSpec

log = logging.getLogger("docproof.quest.skin")

LUNA_MODEL = "gpt-5.6-luna"

# How much of the book the model sees. Opening carries voice and register;
# the middle slice guards against front matter and slow prologues.
OPENING_WORDS = 3000
MIDDLE_WORDS = 1500

# Structured replies on a reasoning model share max_tokens with thinking, and a
# truncated structured reply parses as nothing — so leave far more room than
# the skin itself needs.
MAX_OUTPUT_TOKENS = 8000

# The permanent party: (key, true name, role, what the lane actually does).
# The model writes costumes for these six; it cannot add, drop, or rename the
# underlying identities.
ROLES = (
    ("pip", "Pip", "Scout",
     "finds typos and misspelled words"),
    ("bram", "Bram", "Knight",
     "enforces grammar and punctuation rules"),
    ("maple", "Maple", "Archivist",
     "keeps names, spellings, and capitalization consistent across the book"),
    ("cinder", "Cinder", "Blacksmith",
     "repairs badly broken or garbled sentences"),
    ("sage", "Sage", "Wizard",
     "tracks whole-book continuity (eye colors, timelines, who knew what "
     "when)"),
    ("lark", "Lark", "Bard",
     "suggests optional line-level style improvements, as questions only"),
)

TEXT_SUFFIXES = {".txt", ".md"}


class SkinError(RuntimeError):
    """A skin that cannot be generated at all — unreadable file, no provider.
    Carries a sentence meant for a person to read. A model failure is NOT this:
    that path returns the fallback skin instead, because the page must render."""


@dataclass(frozen=True)
class SkinResult:
    """One generated costume plus everything the caller wants to log or price."""
    skin: SkinSpec
    title: str                    # file stem, the identifier of record
    word_count: int
    band: float                   # price multiplier for this length
    model: str
    cost: float | None            # dollars for this one call, None off-catalog
    fallback: bool                # True when skin is DEFAULT_SKIN
    error: str | None             # why, when fallback is True
    # Aliases that literally appear in the sample — the model may have dressed
    # the party as the book's own characters, which reads as confusing rather
    # than clever. Surfaced for a regenerate/QA decision, not fatal.
    alias_collisions: tuple[str, ...] = ()


def price_band(word_count: int) -> float:
    """The length multiplier the party builder applies to adventurer prices.
    Bands, not a linear rate, so the page shows round numbers."""
    if word_count < 60_000:
        return 0.7
    if word_count <= 120_000:
        return 1.0
    return 1.5


def read_sample_source(path: str | Path) -> Manuscript:
    """A manuscript from a .docx (the pipeline's native read) or, for the
    wide-net trial path, a plain-text file read as one paragraph per line."""
    path = Path(path)
    if path.suffix.lower() in TEXT_SUFFIXES:
        raw = path.read_text(encoding="utf-8", errors="replace")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        text = "\n".join(lines)
        return Manuscript(source_path=str(path),
                          title=path.stem or "manuscript", text=text,
                          word_count=len(text.split()))
    return read_manuscript(path)


def sample_text(text: str) -> str:
    """Opening plus a middle slice, labeled so the model knows the seam."""
    words = text.split()
    opening = " ".join(words[:OPENING_WORDS])
    if len(words) <= OPENING_WORDS + MIDDLE_WORDS:
        return f"OPENING SAMPLE:\n{opening}"
    mid = len(words) // 2
    middle = " ".join(words[mid:mid + MIDDLE_WORDS])
    return (f"OPENING SAMPLE:\n{opening}\n\n"
            f"SAMPLE FROM THE MIDDLE OF THE BOOK:\n{middle}")


def _system_prompt() -> str:
    roles = "\n".join(
        f'- key "{key}": {name} the {role} — {function}.'
        for key, name, role, function in ROLES)
    return f"""You write the "skin" for Spell & Check, a proofreading service \
that presents itself as a tiny adventuring party, led by Galley, an AI guide, \
working through the author's manuscript. You will be shown samples from one \
manuscript. Fill the JSON schema with a costume tailored to that book's genre, \
tone, themes, and maturity.

The permanent party members (never add, drop, or reorder them):
{roles}

Rules:
- alias: an invented in-register name for each member (e.g. noir: Pip becomes \
"Slim"; regency romance: Bram becomes "Lord Bramwell"; fae court: Maple might \
become "Madame Maplewood of the Records"). ALWAYS invent aliases — riff on the \
true name where you can, so the member stays recognizable. The one exception: \
plain sword-and-campfire epic fantasy, where the true names are already at \
home and may stay. NEVER use the name of an actual character, place, or \
person appearing in the manuscript.
- job: ONE sentence in the book's register describing that member's real \
function (given above). Charming, but never misleading about what it does.
- narration: Galley's greeting, 1–3 sentences. Reference two or three concrete, \
non-spoiler details from the opening so the author feels the book was actually \
read. Warm, a little wry. Never quote more than a few words verbatim.
- empty_party / empty_bench: one short line each for an empty roster zone and \
a fully-hired one, in register.
- signoff: one line for how we'll notify the author, as an in-world delivery \
joke ending with "(okay, an email)".
- book_title: the book's actual title if the text reveals one, else a faithful \
short description like "your fantasy novel". Never invent a title.
- genre: 2–4 words naming the genre/mood (e.g. "cozy mystery", "grimdark \
fantasy", "spicy romantasy").
- maturity: "cozy", "standard", or "mature" — judged from the content. This \
tunes how knowing the copy may be. Regardless of the book's content, all copy \
you write stays tasteful and non-explicit.
- palette: the nearest mood — "ember" (fantasy/adventure), "rose" \
(romance/romantasy), "rain" (crime/thriller/dark), "honey" (cozy/gentle/comic).
- language: the manuscript's language. Write ALL skin copy in English.
- is_fiction: false for nonfiction, memoir, poetry collections. Still write a \
good skin — costume the party for the real subject matter, gently.
- themes: 2–4 short phrases naming what the book is about, for logging.

If the sample is disturbing, hateful, or explicit, do not mirror that in the \
copy: pick the closest respectful register and keep every line something a \
stranger could read over the author's shoulder."""


def _user_prompt(ms: Manuscript) -> str:
    return (f"Manuscript file name: {ms.title}\n"
            f"Word count: {ms.word_count:,}\n\n"
            f"{sample_text(ms.text)}")


def _collisions(skin: SkinSpec, sample: str) -> tuple[str, ...]:
    """Aliases that appear verbatim in the sample — likely the book's own
    characters. True names (Pip, Bram, …) are exempt: staying home is allowed."""
    true_names = {name for _, name, _, _ in ROLES}
    hits = []
    for key, _, _, _ in ROLES:
        alias = getattr(skin, key).alias.strip()
        if alias and alias not in true_names and alias in sample:
            hits.append(alias)
    return tuple(hits)


def generate_skin(path: str | Path, provider: Provider, *,
                  model: str = LUNA_MODEL) -> SkinResult:
    """Read a sample of the manuscript and costume the party for it.

    Raises SkinError (via IngestError passthrough at the caller's choice) only
    when the file itself cannot be read. A model that fails, refuses, or
    returns junk is not an error the author should ever see: those paths return
    DEFAULT_SKIN with `fallback=True` and the reason in `error`."""
    ms = read_sample_source(path)
    if not ms.text.strip():
        raise SkinError(f"{Path(path).name} contains no readable text.")
    user = _user_prompt(ms)
    usage = Usage()
    skin: SkinSpec = DEFAULT_SKIN
    error: str | None = None
    try:
        result = provider.complete_structured(
            model=model,
            system=_system_prompt(),
            user=user,
            schema=strict_json_schema(SkinSpec),
            schema_name="quest_skin",
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            error = (f"The model did not return a skin: "
                     f"{result.error or result.stop_reason}.")
        else:
            skin = SkinSpec.model_validate(result.parsed)
    except ValidationError as e:
        error = f"The model's answer did not match the skin schema: {e}"
        skin = DEFAULT_SKIN
    except Exception as e:  # noqa: BLE001 - SDK/network variants; page must render
        error = f"The skin call failed: {e}"
    if error:
        log.warning("Skin fallback for %s: %s", ms.title, error)
    collisions = () if error else _collisions(skin, ms.text)
    if collisions:
        log.info("Skin aliases collide with the text of %s: %s", ms.title,
                 ", ".join(collisions))
    return SkinResult(
        skin=skin, title=ms.title, word_count=ms.word_count,
        band=price_band(ms.word_count), model=model,
        cost=cost_of_usage(usage, fallback_model=model),
        fallback=error is not None, error=error,
        alias_collisions=collisions)
