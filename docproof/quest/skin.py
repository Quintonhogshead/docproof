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
    # Historical field: invented aliases are gone (names are permanent), so
    # this is always empty. Kept so the /api/quest/skin payload stays stable.
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
- alias: always exactly the member's true first name — "Pip", "Bram", \
"Maple", "Cinder", "Sage", "Lark". The names never change, in any genre; all \
costuming lives in job and look. Never rename, extend, or title a member.
- job: ONE sentence in the book's register describing that member's real \
function (given above). Charming, but never misleading about what it does.
- look: ONE sentence describing that member's appearance in this book's world, \
written for an illustrator — age impression, build, attire, one memorable \
detail. Keep each member's permanent silhouette: Pip small and quick; Bram \
broad and steady; Maple precise and bespectacled; Cinder strong-armed with \
tools; Sage old and unhurried; Lark bright-eyed with an instrument or notebook. \
Never dress a member as, or name them after, an actual character in the \
manuscript. \
When cultural grounding (below) applies, say plainly and respectfully that the \
member belongs to that community — an illustrator cannot draw an implication.
- Cultural grounding: when the manuscript is rooted in a specific culture, \
community, place, or era that is CENTRAL to the story (a novel of the Black \
American South, a Lagos family saga, a Punjabi wedding comedy, an Appalachian \
holler, a Deaf community memoir), let the party belong to that world — names, \
titles, attire, and idiom that would feel like neighbors inside the book, \
drawn with the same dignity the manuscript draws its own people. Ground every \
detail in what the text itself shows. Never use generic ethnic shorthand, \
dialect imitation, or sacred/religious roles as costume. When the culture is \
incidental rather than central, dress by genre alone.
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
- palette: the nearest mood — "ember" (epic fantasy/adventure), "rose" \
(romance/romantasy), "rain" (crime/noir/thriller), "honey" (cozy/gentle/comic), \
"void" (science fiction/space), "neon" (cyberpunk/urban fantasy/near-future), \
"verdigris" (nautical/pirate/historical adventure), "bone" (horror/gothic), \
"gold" (myth/royal intrigue/epic history), "slate" (literary/contemporary), \
"rust" (western/frontier/post-apocalyptic), "frost" (winter fantasy/nordic).
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


def _true_names(skin: SkinSpec) -> SkinSpec:
    """The party's names never change (product decision 2026-08-28): whatever
    the model wrote in `alias`, the page shows Pip, Bram, Maple, Cinder, Sage,
    and Lark. Enforced here rather than trusted to the prompt, so a creative
    reply can never rename anyone."""
    return skin.model_copy(update={
        key: getattr(skin, key).model_copy(update={"alias": name})
        for key, name, _, _ in ROLES})


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
    return SkinResult(
        skin=_true_names(skin), title=ms.title, word_count=ms.word_count,
        band=price_band(ms.word_count), model=model,
        cost=cost_of_usage(usage, fallback_model=model),
        fallback=error is not None, error=error)
