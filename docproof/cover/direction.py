"""The two model calls that make Cover Studio a designer rather than a
prompt box: art direction (a Brief becomes several distinct design concepts)
and revision (a CoverSpec plus a human's notes becomes an edited CoverSpec).

Both mirror docproof/quest/skin.py's structure and safety habits — one
structured call through the Provider protocol, strict_json_schema, a generous
MAX_OUTPUT_TOKENS because a truncated structured reply on a reasoning model
parses as nothing, cost_of_usage for pricing — with one deliberate
difference: skin.py degrades to DEFAULT_SKIN on a bad reply because the page
must always render; there is no such fallback here. A junk direction goes on
to spend real image-generation dollars, and a junk revision would silently
corrupt a spec a human is iterating on, so both calls raise instead — an
unreadable-sentence error, never a guess dressed up as a result. LUNA_MODEL is
mirrored rather than imported from docproof.quest.skin on purpose: the two
features share a convention, not a dependency (the same reasoning
docproof.cover.archetypes gives for mirroring Zone/Shadow/Stroke instead of
importing docproof.cover.model).

The art-direction call also narrows which archetypes it enumerates to the
brief's own genre (§5.3): `_normalize_genre` turns `brief.genre` into an
exact docproof.cover.archetypes.SUBJECT_KEYS match or None, and
`describe_archetypes(genre)` does the actual filtering. This only shrinks
what the model is SHOWN, never what it may legally pick — `_validate_direction`
below still checks a chosen archetype against the full, unfiltered
`ARCHETYPES`, because a concept is always free to reach for one of the
untagged, fits-every-genre launch archetypes even when the enumerated list
was narrowed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from ..models import Usage
from ..providers import Provider, cost_of_usage, strict_json_schema
from .archetypes import ARCHETYPES, SUBJECT_KEYS, describe_archetypes
from .fonts import describe_fonts
from .model import ArtSlot, Brief, CoverSpec, Direction, Directions

# Unlike docproof.quest.skin (which logs a fallback and keeps going, since a
# skin failure must never stop the page from rendering), every failure path
# in this module raises. The exception's own sentence IS the record — the
# caller (docproof.cover.pipeline) is what decides whether/how to log it
# against a job, the same way docproof.cover.archetypes' ArchetypeError and
# docproof.promo.ingest's IngestError raise without logging here either.

log = logging.getLogger("docproof.cover.direction")

LUNA_MODEL = "gpt-5.6-luna"

# Structured replies on a reasoning model share max_tokens with thinking, and
# a truncated structured reply parses as nothing — so leave far more room
# than either call needs (a Directions answer or a whole CoverSpec both fit
# comfortably under this).
MAX_OUTPUT_TOKENS = 8000


class DirectionError(RuntimeError):
    """The art-direction call failed outright, or came back with directions
    that cannot be trusted: an unreadable model failure, a schema mismatch, a
    concept count that doesn't match what was asked for, or a concept naming
    an archetype or art slot that does not exist. Unlike
    docproof.quest.skin.SkinError, there is no default-direction fallback —
    a junk direction would go on to spend real image-generation dollars on
    nonsense, so a bad call must stop the job rather than limp forward.
    Always carries a sentence meant for a person to read."""


class RevisionError(RuntimeError):
    """A revision call that failed outright, or came back invalid. The
    caller keeps the prior version of the spec and shows this sentence — a
    failed revision must never lose or corrupt the version already on
    file."""


@dataclass(frozen=True)
class DirectionResult:
    """One art-direction call's whole answer, plus what it cost."""
    directions: list[Direction]
    model: str
    cost: float | None


@dataclass(frozen=True)
class RevisionResult:
    """One revision call's edited spec, plus what it cost. `spec` already
    carries the bumped version, the appended notes_log entry, and cleared
    assets on any changed art slot — see revise_spec's docstring."""
    spec: CoverSpec
    cost: float | None


# -- art direction: brief -> N distinct concepts (spec §6.1) -----------------

def _normalize_genre(genre: str) -> str | None:
    """The brief's genre is free text OR one of the ten subject keys (see
    docproof.cover.model.Brief) — only an EXACT match narrows the archetype
    enumeration below; anything else (free text, a typo, different casing)
    normalizes to None, which asks describe_archetypes() for the unfiltered
    list. describe_archetypes() itself falls back the same way for any
    genre it doesn't recognize (§5.3), so this is a belt-and-suspenders
    normalization, not the only thing standing between a messy brief and a
    broken prompt — but it keeps the rest of this module thinking in terms
    of "a real subject key, or nothing" rather than "whatever the human
    typed.\""""
    return genre if genre in SUBJECT_KEYS else None


def _big_type_rule(n: int) -> str:
    if n < 3:
        return ""
    return (
        "\n- At least one concept MUST use the big_type archetype. It costs "
        "nothing to render (no generated art required), it is the most "
        "print-safe archetype in the whole library, and it is often simply "
        f"the best answer — with {n} concepts to fill, one should always be "
        "it.")


def _sample_rule(has_sample: bool) -> str:
    if not has_sample:
        return ""
    return (
        "\n\nA MANUSCRIPT SAMPLE follows the brief below. Ground imagery, "
        "mood, and palette in the manuscript's actual text — its era, "
        "setting, and the concrete details it shows — not just the genre "
        "label. The brief's own typed fields (title, author, genre, and "
        "every other labeled field above) ALWAYS win over the sample on any "
        "conflict. Never spoil an ending on the cover.")


def _direction_system_prompt(n: int, *, has_sample: bool,
                             genre: str | None) -> str:
    return f"""You are a senior book-cover art director at a traditional \
press. You will be given a brief for one book — title, author, genre, pitch, \
mood, and constraints — and you write {n} distinct DESIGN CONCEPTS for its \
cover: archetype, palette, fonts, and art direction. You are not writing one \
finished image prompt; you are art-directing {n} different approaches.

Each concept must be genuinely distinct from every other concept in this \
same answer: a different archetype, OR a sharply different palette and \
imagery. Two concepts that are the same idea in different words are a \
failure — vary the approach, not just the adjectives.

Honor genre conventions by default — a romance and a horror novel should \
never read as the same cover dressed in different colors — but the brief's \
own `mood` and `avoid` fields override genre convention whenever they say \
something specific.

Archetypes — pick exactly one per concept, by its exact key. The list below \
is already narrowed to this book's genre plus every archetype that fits any \
genre — pick from it exactly as if it were the whole library, never a name \
outside it:
{describe_archetypes(genre)}{_big_type_rule(n)}

Fonts — pick title_font and author_font per concept, each from this exact \
list; nothing outside it is valid:
{describe_fonts()}

Palette: five hexes by role (background, primary, accent, text, scrim). \
Choose real contrast intent, not just a pretty scheme — `text` must be \
readable over `background` combined with `scrim` at typical strength. (The \
composer enforces this mechanically later and will escalate the scrim or \
flip the text color if you get it wrong, but a good answer gets there on \
its own.)

art_prompts: a list with one {{slot, prompt, treatment}} entry for every \
generatable art slot the archetype you picked declares. Each prompt is 1–3 \
sentences describing subject, style, \
lighting, era, and medium ("flat vector", "oil painting", "photographic", \
"paper-cutout collage", and so on). Rules for every art prompt, no \
exceptions: never ask for text, letters, numbers, typography, book covers, \
mockups, borders, or frames; never name a living artist; describe a scene, \
not a cover. Prefer illustrated, painterly, or graphic media (oil painting, \
gouache, flat vector, linocut, paper-cutout collage, and the like) over \
photorealism; ask for a photographic or photoreal render only when the \
brief explicitly calls for photography — stylized media hide generation \
artifacts, and photoreal is the single biggest "AI-generated" tell. The \
composition note that keeps room for the type is appended by code \
afterward — do not write it yourself.

The effects rack: `treatment` on an art_prompts entry is a deterministic, \
$0 post-processing pass the composer applies to that slot after it is \
generated — "none" (the default — leave it alone) unless the archetype's \
own convention or the brief's mood specifically earns one of: "duotone" \
(maps the art onto a two-color background/primary ramp — flat graphic and \
color-block conventions want this), "silhouette" (thresholds the art to one \
flat primary shape — thriller and historical-figure conventions want this), \
"posterize" (snaps the art to four flat palette colors — a bold poster-\
graphic look), or "sticker" (outlines a transparent cutout with a text-\
colored edge — collage looks want this). `treatment` is the ONLY \
effects-rack field you ever set. Mirrored corners (ornamental-frame \
conventions), motif scatter (repeating-pattern conventions), double-\
exposure masking, and knockout/art_fill title treatments (used only when \
the archetype's type IS the hero of the cover) are archetype and revision \
territory — never invent or request them yourself; pick the archetype whose \
own convention already wants one, and trust it to carry that.

If the brief's `pitch` is present, ground the imagery in it. Never spoil an \
ending on the cover, regardless of how much the pitch reveals.{_sample_rule(has_sample)}"""


def _direction_user_prompt(brief: Brief, manuscript_sample: str) -> str:
    fields = (
        ("Title", brief.title), ("Subtitle", brief.subtitle),
        ("Author", brief.author), ("Genre", brief.genre),
        ("Pitch", brief.pitch), ("Mood", brief.mood),
        ("Must include", brief.must_include), ("Avoid", brief.avoid),
    )
    body = "\n".join(f"{label}: {value}" for label, value in fields if value)
    if manuscript_sample:
        body += f"\n\nMANUSCRIPT SAMPLE:\n{manuscript_sample}"
    return body


def _validate_direction(direction: Direction) -> Direction:
    """archetype must be real — a fabricated one is fatal, because nothing
    downstream can render it. An art prompt for a slot the archetype does
    not generate is NOT fatal: build_spec simply never reads it, so the
    honest handling is to drop the extra entry and say so, not to kill a
    whole multi-concept job over surplus enthusiasm (a live run died exactly
    this way — one stray `foreground` prompt took down three paid-for
    concepts that were otherwise fine). Dropping costs nothing; the
    keep-only-generatable copy is returned."""
    archetype = ARCHETYPES.get(direction.archetype)
    if archetype is None:
        raise DirectionError(
            f"{direction.concept_name!r} picked archetype "
            f"{direction.archetype!r}, which is not one of the shipped "
            f"archetypes ({', '.join(sorted(ARCHETYPES))}).")
    generatable = {a.id for a in archetype.art if a.generatable}
    extra = sorted({p.slot for p in direction.art_prompts} - generatable)
    if not extra:
        return direction
    log.info("Direction %r wrote art prompts for %s, which the %s archetype "
             "does not generate; dropped.", direction.concept_name,
             ", ".join(extra), archetype.name)
    kept = [p for p in direction.art_prompts if p.slot in generatable]
    return direction.model_copy(update={"art_prompts": kept})


def run_directions(brief: Brief, provider: Provider, *, n: int,
                   manuscript_sample: str = "",
                   model: str = LUNA_MODEL) -> DirectionResult:
    """One structured call for all n design concepts at once (spec §6.1).

    Raises DirectionError on any failure: a call error, a schema mismatch, a
    concept count that doesn't match `n`, or a concept naming an archetype or
    art slot that doesn't exist. There is no fallback (contrast with
    docproof.quest.skin.generate_skin) — a junk direction would go on to
    spend real image-generation dollars."""
    usage = Usage()
    genre = _normalize_genre(brief.genre)
    try:
        result = provider.complete_structured(
            model=model,
            system=_direction_system_prompt(
                n, has_sample=bool(manuscript_sample), genre=genre),
            user=_direction_user_prompt(brief, manuscript_sample),
            schema=strict_json_schema(Directions),
            schema_name="cover_directions",
            max_tokens=MAX_OUTPUT_TOKENS)
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            raise DirectionError(
                f"The model did not return any cover directions: "
                f"{result.error or result.stop_reason}.")
        directions = Directions.model_validate(result.parsed)
    except ValidationError as e:
        raise DirectionError(
            f"The model's directions did not match the schema: {e}") from e
    except DirectionError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise DirectionError(f"The art-direction call failed: {e}") from e

    if len(directions.concepts) != n:
        raise DirectionError(
            f"Asked the model for {n} cover concepts but got "
            f"{len(directions.concepts)}.")
    validated = [_validate_direction(d) for d in directions.concepts]

    return DirectionResult(directions=validated, model=model,
                           cost=cost_of_usage(usage, fallback_model=model))


# -- revision: spec + notes -> edited spec (spec §6.2) ------------------------

def _revision_system_prompt() -> str:
    return """You are editing a book cover design document (a CoverSpec), \
not writing prose. You will be given the current spec as JSON and a human's \
notes about what to change. Change ONLY what the notes require; copy every \
other field through completely unchanged, exactly as given — do not \
rephrase, reformat, or "improve" anything the notes did not ask about.

You may: move or resize zones; change palette hexes; swap fonts (from the \
same closed list already used in the spec — nothing outside it is valid); \
change text case, tracking, or align; adjust scrim strengths and art \
transforms (scale, offset, anchor); rewrite an art_prompt; toggle the \
texture layer on or off.

You may NOT: change `archetype` unless the notes explicitly ask for a \
different archetype; invent a new art, scrim, or text slot that was not in \
the input spec; change a text slot's `content` unless the notes dictate new \
wording; touch any `asset` path (leave every `asset` field exactly as \
given — code decides separately which assets need to be regenerated).

Echo `version` back unchanged — the calling code bumps it, not you. Do not \
add anything to `notes_log` — the calling code appends the notes itself; \
copy the existing `notes_log` array through verbatim."""


def _revision_user_prompt(spec: CoverSpec, notes: str) -> str:
    return (f"Current cover spec (JSON):\n{spec.model_dump_json()}\n\n"
            f"Notes from a human editor — apply exactly these changes and "
            f"nothing else:\n{notes}")


def revise_spec(spec: CoverSpec, notes: str, provider: Provider, *,
                model: str = LUNA_MODEL) -> RevisionResult:
    """One structured call that edits a spec in place (spec §6.2).

    Raises RevisionError on any failure; this function never mutates `spec`
    itself, so the caller keeps the prior version on that path. On success,
    the returned spec's `version` is bumped and `notes` is appended to
    `notes_log` in code — the model's own echo of either field is discarded,
    never trusted — and any art slot whose `prompt` or `transparent` changed
    has its `asset` cleared, which is the signal
    docproof.cover.pipeline.run_revision uses to regenerate exactly that one
    image and no other."""
    usage = Usage()
    try:
        result = provider.complete_structured(
            model=model, system=_revision_system_prompt(),
            user=_revision_user_prompt(spec, notes),
            schema=strict_json_schema(CoverSpec),
            schema_name="cover_revision",
            max_tokens=MAX_OUTPUT_TOKENS)
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            raise RevisionError(
                f"The model did not return a revised cover spec: "
                f"{result.error or result.stop_reason}.")
        revised = CoverSpec.model_validate(result.parsed)
    except ValidationError as e:
        raise RevisionError(
            f"The revised spec did not match the schema: {e}") from e
    except RevisionError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise RevisionError(f"The revision call failed: {e}") from e

    old_art = {a.id: a for a in spec.art}
    new_art: list[ArtSlot] = []
    for slot in revised.art:
        old = old_art.get(slot.id)
        # A brand-new slot id (the model breaking the "no new slots" rule)
        # has no prior asset to restore, so it reads as changed too — there
        # is nothing valid to keep, only something to (re)generate.
        regen = (old is None or old.prompt != slot.prompt
                or old.transparent != slot.transparent)
        new_art.append(slot.model_copy(
            update={"asset": "" if regen else old.asset}))

    final = revised.model_copy(update={
        "version": spec.version + 1,
        "notes_log": [*spec.notes_log, notes],
        "art": new_art,
    })
    return RevisionResult(spec=final,
                          cost=cost_of_usage(usage, fallback_model=model))


__all__ = ["LUNA_MODEL", "DirectionError", "DirectionResult", "RevisionError",
          "RevisionResult", "revise_spec", "run_directions"]
