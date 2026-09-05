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
unreadable-sentence error, never a guess dressed up as a result.

DIRECTION_MODEL / REVISION_MODEL are mirrored rather than imported from
docproof.quest.skin.LUNA_MODEL on purpose: the two features share a
convention, not a dependency (the same reasoning docproof.cover.archetypes
gives for mirroring Zone/Shadow/Stroke instead of importing
docproof.cover.model). They also deliberately split (the BRAIN wave, 2026-08-
29): art direction is the one call worth the frontier model's price — "covers
need to be good," per the owner, and a timid or generic set of concepts
poisons every concept built from it — while a revision (human-triggered or
the auto-critique loop in docproof.cover.pipeline) is a narrower, cheaper
edit-this-document task that a mid-tier model handles well. See each
constant's own comment.

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

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..models import Usage
from ..providers import Provider, cost_of_usage, strict_json_schema
from . import doctrine
from .archetypes import ARCHETYPES, SUBJECT_KEYS, describe_archetypes
from .fonts import describe_fonts
from .model import ArtSlot, Brief, CoverSpec, Direction, Directions
from .recipes import describe_recipes

# Unlike docproof.quest.skin (which logs a fallback and keeps going, since a
# skin failure must never stop the page from rendering), every failure path
# in this module raises. The exception's own sentence IS the record — the
# caller (docproof.cover.pipeline) is what decides whether/how to log it
# against a job, the same way docproof.cover.archetypes' ArchetypeError and
# docproof.promo.ingest's IngestError raise without logging here either.

log = logging.getLogger("docproof.cover.direction")

# The frontier model: art direction is the one call in Cover Studio where
# quality is worth paying for (owner decision, 2026-08-29 — "covers need to
# be good"; roughly $1-2 per 6-concept job at typical prompt sizes, which the
# owner accepted). A real docproof.providers.catalog id — see that module for
# current pricing.
DIRECTION_MODEL = "claude-fable-5"

# The workhorse model: a revision (§6.2) is shown a document it is already
# handed in full and answers with a short list of patch edits against it —
# a narrower task than drafting concepts from nothing, and not worth the
# frontier price. Human-triggered only now; the auto-critique loop that also
# called it was replaced by docproof.cover.atelier.
REVISION_MODEL = "claude-sonnet-5"

# Structured replies on a reasoning model share max_tokens with thinking, and
# a truncated structured reply parses as nothing — so leave far more room
# than either call needs (a Directions answer or a SpecEdits patch list both
# fit comfortably under this).
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
    assets on any changed art slot — see revise_spec's docstring. `skipped`
    names every edit the model asked for that was refused before it ever
    reached CoverSpec validation: a guarded path (`version`, `notes_log`, an
    art slot's `asset`), a path that doesn't parse, or one that doesn't
    resolve against the spec it was shown — see _apply_edit. Empty on a
    call where every edit (if any) applied cleanly. Added after
    RevisionResult already shipped, so it carries a default and every
    existing keyword-constructed caller (pipeline.py's frozen-dataclass
    consumers, this module's own tests) keeps working unchanged."""
    spec: CoverSpec
    cost: float | None
    skipped: tuple[str, ...] = ()


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
    """docproof.cover.pipeline.run_job distills a manuscript sample into a
    compact REALITY SHEET (docproof.cover.reality) before this call ever
    runs, and hands the SHEET's rendered text in as `manuscript_sample` —
    same parameter, so the prompt shape below barely changes. The one
    exception is a distillation failure, which falls back to the raw sample
    (reality.py's caller logs and ledgers it, never blocks the job on it) —
    this rule has to read sensibly for either shape landing in the
    MANUSCRIPT SAMPLE section below, so it names both without committing to
    which one arrived."""
    if not has_sample:
        return ""
    return (
        "\n\nA MANUSCRIPT SAMPLE follows the brief below — usually a "
        "distilled REALITY SHEET pulled from the book (its setting, era, "
        "palette cues, concrete objects, motifs, atmosphere, and anything "
        "the cover must never show), occasionally the manuscript's own raw "
        "opening text on the rare run where a sheet could not be produced. "
        "Either way, ground imagery, mood, and palette in the manuscript's "
        "actual text — its concrete objects and motifs, not just the genre "
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
list; nothing outside it is valid. The list is grouped by ROLE, and each \
group's own gloss names the shelf it belongs to — pick the role this \
genre's shelf actually uses, then the family whose vibe line fits the \
concept, honoring any "pairs with" hint for the author line:
{describe_fonts()}

token_layout: where a template's two accent plates sit, from the closed \
list far_high_left / far_high_right / far_low_left / far_low_right (or \
"" to keep the template's own default). It applies only to archetypes \
that declare a token_far/token_near pair and is ignored everywhere else. \
Name says where the SMALLER, further token sits; the larger, nearer one \
answers from the opposite corner. Vary it across concepts and across \
books: the placement is the one part of such a template's arrangement \
you can move, and leaving every cover on the same layout is what makes a \
list of books built on one template read as one cover reprinted. Pick \
the corner that leaves the subject's own silhouette and the light source \
uncrowded.

text_overrides: per-BOOK typography, as {{slot: {{...}}}} keyed by "title", \
"author", "subtitle" or "series". Any of zone {{x, y, w, h}} as 0-1 fractions \
of the cover, plus align, valign, case, tracking, max_lines, size_min, size_max, \
color_role (background/primary/accent/text/scrim) and font_family. Omit it \
entirely, or leave a field out, to keep what the archetype ships — its zones \
are tuned and they are a good default. Reach for it when THIS book's art \
argues with them: the byline sitting on the busiest part of the plate, a \
tagline landing in the one bright corner, a long title needing a taller \
band, or a colour role that disappears into this palette. Move type to the \
quiet region this particular cover has, and vary it across concepts and \
across books — four books on one template with the title, byline, subtitle \
and tagline in the same four spots read as one cover reprinted.

Palette: five hexes by role (background, primary, accent, text, scrim). \
Grounds must be FLAT and CONFIDENT, never muddy: commit to a single color \
(cream, or one saturated hue) or a lightly textured field, and use 2–4 \
colors per cover, never more. Tone-on-tone monochrome — dark red art on a \
red ground, say — is a signature move to reach for on purpose, not a \
compromise. Muddy gray-on-gray and low-saturation-everything palettes read \
as timid and are a failure, unless the brief itself specifically demands \
that muted a mood. Within that discipline, choose real contrast intent, \
not just a pretty scheme — `text` must be readable over `background` \
combined with `scrim` at typical strength. (The composer enforces this \
mechanically later and will escalate the scrim or flip the text color if \
you get it wrong, but a good answer gets there on its own.) A dark, quiet \
cover must still carry one high-voltage element — an accent hue doing \
real work on the silhouette, a type line, or a single motif; \
all-values-within-a-whisper is a failure.

art_prompts: a list with one {{slot, prompt, treatment}} entry for every \
generatable art slot the archetype you picked declares. The archetype list \
above names each archetype's art slots BY EXACT ID — use those ids, spelled \
exactly, and no others: a prompt for a slot the archetype does not declare \
is silently discarded, and a declared slot you skip ships with no art at \
all. Each prompt is 1–3 \
sentences describing subject, style, \
lighting, era, and medium ("flat vector", "oil painting", "photographic", \
"paper-cutout collage", and so on). Rules for every art prompt, no \
exceptions: never ask for text, letters, numbers, typography, book covers, \
mockups, borders, or frames; never name a living artist; describe a scene, \
not a cover. For a symbolic-object slot, direct ONE clean, instantly \
recognizable object, described plainly (a brass key, a single feather); \
never a surreal composite, never anatomy on inanimate things.

EVERY NOUN YOU SUPPLY MUST COME FROM THIS BOOK. Each slot is listed with \
its ROLE after a dash — a role naming a figure, character, lead or \
subject wants THIS BOOK'S ACTUAL PEOPLE: the characters the text is about, \
described as the text describes them (their age, build, colouring, dress, \
what they carry, what they are marked by), never a generic hooded figure, \
never a stock warrior or sorceress, never a stand-in assembled from the \
genre at large. A role naming a ground, material, border or motif wants a \
material or motif the book itself names. Where an archetype supplies a \
prompt frame, that frame owns the STRUCTURE — the pose, the crop, the \
lighting, which edge a cut sits on — and you own only the nouns dropped \
into it; do not restate or fight the structure, and do not surrender the \
nouns to it. A cover whose figures are not recognisably this book's \
characters has failed, however well it renders. Prefer \
illustrated, painterly, or graphic media (oil painting, \
gouache, flat vector, linocut, paper-cutout collage, and the like) over \
photorealism. A photographic or photoreal render is permitted ONLY when \
paired with treatment: "photo_soft" (the dedicated blur + desaturate + \
duotone-ramp + grain treatment that makes a photograph shelf-safe) or with \
treatment: "duotone"/"silhouette" — an untreated (treatment: "none") \
photographic prompt is never allowed, brief or no brief. Stylized media \
hide generation artifacts, and a raw, untreated photoreal image is the \
single biggest "AI-generated" tell. THE ONE EXEMPTION: an archetype marked \
[PHOTOREAL TEMPLATE] in the list above is photographic by construction and \
carries its own discipline instead — its composition note fixes the medium, \
the key light and the saturation identically for every plate, and its \
finishing recipe grades, blooms and grains them onto one piece of film. On \
those archetypes, and ONLY those, prompt every plate photoreal and leave \
treatment "none": photo_soft there is a mistake, not a safety net, because \
it is a duotone and will flatten the whole cover into one sepia mass. On \
every other archetype the rule above stands unchanged. The composition note \
that keeps room for the type is appended by code afterward — do not write \
it yourself.

Image simplicity is not a vague preference — simple shapes and silhouettes \
hide AI-generation artifacts far better than rendered detail does. This \
ladder does not apply on a [PHOTOREAL TEMPLATE], whose plates are \
photographs by construction; there, describe the real thing in real light \
and let the template's own note and finishing recipe do the unifying. \
Everywhere else, for every generatable art prompt, reach for the SIMPLEST \
form on this list that still serves the concept, in order:
1. Flat SILHOUETTE — one flat color, a pure shape, no interior detail at \
all (pair it with treatment: "silhouette").
2. DUOTONE single subject — one subject rendered tone-on-tone against the \
flat ground (pair it with treatment: "duotone").
3. ORNAMENT/PATTERN — a repeating border, botanical flourish, emblem, or \
motif AS THE ART ITSELF (not a frame drawn around a finished cover — that \
rule above still stands).
4. Stylized single-subject illustration — one subject, still simplified, \
with some interior rendering.
5. A full illustrated scene — ONLY when the brief explicitly demands one, \
and never a sweeping or complex vista.
If a concept reaches past level 3, its `rationale` must say why, in one \
clause.

Write every silhouette or cutout art prompt around SHAPE and GESTURE, not \
surface detail — "a wing swept upward," "a beam widening from the tower \
base," never the texture of feathers or brickwork. Rendered detail is \
exactly where a generator's fingerprints show; a clean, simple gesture \
hides them.

Type is the hero of the cover: choose the archetype and palette so the \
title can run huge and confident. When a concept's font, palette, and \
archetype could support a bolder, larger title, take the bolder choice.

The effects rack: `treatment` on an art_prompts entry is a deterministic, \
$0 post-processing pass the composer applies to that slot after it is \
generated — "none" (the default — leave it alone) unless the archetype's \
own convention or the brief's mood specifically earns one of: "duotone" \
(maps the art onto a two-color background/primary ramp — flat graphic and \
color-block conventions want this), "silhouette" (thresholds the art to one \
flat primary shape — thriller and historical-figure conventions want this), \
"posterize" (snaps the art to four flat palette colors — a bold poster-\
graphic look), "sticker" (outlines a transparent cutout with a text-\
colored edge — collage looks want this), or "photo_soft" (blurs, fully \
desaturates, and maps a photographic image through the same duotone ramp \
plus a light grain overlay — the treatment that makes a photographic or \
photoreal prompt allowed at all on an ordinary archetype, and a mistake on \
a [PHOTOREAL TEMPLATE]; see the photorealism rule above). \
`treatment` is the ONLY effects-rack field you ever set. Mirrored corners \
(ornamental-frame \
conventions), motif scatter (repeating-pattern conventions), and \
knockout/art_fill title treatments (used only when \
the archetype's type IS the hero of the cover) are archetype and revision \
territory — never invent or request them yourself; pick the archetype whose \
own convention already wants one, and trust it to carry that. (Masking is \
the one exception, and it has its own closed vocabulary — see "Masking \
moves" below.)

The container device: the single strongest intentionality move available \
on a cover is content living inside a shape that belongs to the scene — a \
lighthouse's beam, a train's smoke plume, a ribbon, a banner, a doorway, a \
keyhole, a mirror, a wave. Some archetypes declare a container slot for \
exactly this: a transparent cutout whose shape will clip the title (or \
another image) once composed. When the archetype you picked declares one, \
write that slot's art prompt as ONE clean, bold shape with a soft, simple \
interior — the payload has to read clearly once it sits inside — for \
example "a lighthouse's light beam as a wide, soft-edged cone of pale \
light, transparent background." Never describe or bake the payload (the \
title, the second image) into the container image itself; the container is \
a shape only, and the composer fills it.

The finishing recipe: `recipe` names ONE researched, $0 finishing stack — \
grade, grain, vignette, and texture layers the composer expands over the \
whole composition, text included, after everything else is drawn. It is \
how a cover gets its printed, unified, lit-as-one-scene surface without a \
single extra image generation. Pick the recipe whose look matches this \
genre's shelf, from this exact list, or "" for none:
{describe_recipes()}
Pick "" when the archetype's own look is already complete — an archetype \
may carry a default finishing stack of its own, and "" lets that default \
apply, while a named pick always wins over it. Restraint rule of thumb: \
big_type usually wants quiet_literary or nothing.

The type move: `type_move` may request ONE signature typography move on \
the title — one move per concept is a hard rule, and "" (no move, the \
default) is the most common right answer; restraint is what separates a \
signature move from decoration. The moves: "justify_stack" (each title \
line sized independently so every line fills the zone's width — the \
nonfiction/thriller poster stack; earns its place when the title's words \
break into naturally uneven lines), "arch" (a gentle upward bow along a \
curved baseline — emblem, stamp, and vintage-label conventions), "tilt" \
(a slight confident tilt of the whole title — playful, pulpy, or \
off-kilter moods), and "emphasis" (ONE word of the title styled in the \
accent color — also set `emphasis_word` to that word exactly as it \
appears in the title; a word the title does not contain is dropped). \
When you pick any move other than "emphasis", leave `emphasis_word` \
empty — it rides only with "emphasis", never as a second move.

Masking moves: masks are how real covers earn their "a designer composed \
this" depth, and there are exactly four. (1) PLATE-BLEND — two full-bleed \
plates dissolved into one scene along a soft gradient seam; it earns its \
place when no single image can hold both of the book's worlds (a skyline \
over a forest, a face over the sea). (2) THING-IN-TEXT — the art living \
INSIDE the title's own letterforms; it earns its place when type is the \
hero and the imagery works as texture and color rather than as a subject \
to be read. (3) TEXT-IN-THING — the title clipped inside a shape that \
belongs to the scene (the container device above); it earns its place \
only when the archetype declares a container slot, and the archetype \
carries it for you — never request it yourself. (4) REGION-GRADE — a \
color grade masked to one region so one area darkens or cools without \
touching the rest; archetype and revision territory, never yours to \
request. At direction time you reach the reachable moves through ONE \
field: `mask_intent` on an art_prompts entry — "" (none, the default), \
"blend_into_background" (plate-blend: this slot dissolves into the \
background plate behind it), "inside_title" (thing-in-text: this slot's \
art shows only through the title's letterforms), or "inside_focal" (this \
slot's art lives inside the focal slot's own silhouette — the classic \
double-exposure move, only for an archetype whose focal is a cutout \
drawn beneath this slot; anything the archetype cannot honor is simply \
dropped). Required with any inside_* intent: write that slot's prompt as \
a clean TRANSPARENT-BACKGROUND cutout subject — a windowed clip only \
reads when the source's own shape is clean.

Those three fields — `recipe`, `type_move`, and `mask_intent` — plus \
per-slot `treatment` are your WHOLE design-machinery vocabulary. You \
never set adjust-layer, mask, or effect fields directly at direction \
time: the recipe expands into real graded layers for you, and everything \
finer is archetype and revision territory.

If the brief's `pitch` is present, ground the imagery in it. Never spoil an \
ending on the cover, regardless of how much the pitch reveals.\
{_sample_rule(has_sample)}

THE HOUSE DOCTRINE. Every rule below was learned by shipping a cover that \
broke it, and they bind a concept at the moment you write it — a concept \
that asks for a standing cutout without saying what it stands on has already \
failed, and no amount of downstream work recovers it. Numbering is the \
house's own and is stable across the studio, so gaps are expected.

{doctrine.render("direction")}"""


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
                   model: str = DIRECTION_MODEL) -> DirectionResult:
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


# Revisions use bounded path/value patches so the wire schema stays small as
# CoverSpec grows; the patched document is validated as a complete spec.

# A path segment is a field name (lowercase letters/underscore) with an
# optional trailing `[n]` list index — `text[1]` tokenizes to `"text"` then
# `1`; a bare `layers` tokenizes to just `"layers"`. Matches _parse_path's
# own docstring and spec §6.2's "tokens ^[a-z_]+$ plus [int]".
_PATH_SEGMENT_RE = re.compile(r"^([a-z_]+)(\[(\d+)\])?$")


class SpecEdit(BaseModel):
    """One patch edit against the spec the model was shown. `path` locates
    a field — dotted keys, optional `[n]` list indices, see
    _revision_system_prompt's worked examples. `value` is the new value
    there, JSON-encoded as a plain string ('"#a83250"', '0.13',
    '[0.5, 1.0]', '{"x":0.1,...}') so the wire shape stays a flat pair of
    strings no matter what kind of field is being patched — see
    _apply_edit for how it's decoded and placed."""
    model_config = ConfigDict(extra="forbid")

    path: str
    value: str


class SpecEdits(BaseModel):
    """One revision call's whole answer: a small list of patch edits
    against the spec it was shown, never the document itself. This is the
    entire wire schema for the revision call now — see this section's own
    header comment for why."""
    model_config = ConfigDict(extra="forbid")

    edits: list[SpecEdit] = Field(default_factory=list, min_length=0,
                                  max_length=40)


def _parse_path(path: str) -> list[str | int] | None:
    """Tokenize a SpecEdit.path into a list of dict keys and list indices:
    `text[1].zone.y` -> `["text", 1, "zone", "y"]`; `layers` -> `["layers"]`.
    None when the path doesn't match the syntax at all — as opposed to
    matching but not resolving against the current spec, which
    _resolve_container decides once the spec's actual shape is in hand."""
    tokens: list[str | int] = []
    for segment in path.split("."):
        m = _PATH_SEGMENT_RE.match(segment)
        if not m:
            return None
        tokens.append(m.group(1))
        if m.group(3) is not None:
            tokens.append(int(m.group(3)))
    return tokens


def _is_guarded(tokens: list[str | int]) -> bool:
    """`version`, `notes_log`, and any art slot's `asset` are code's alone
    to write (§6.2) — the calling code bumps version and appends notes_log
    unconditionally, and revise_spec recomputes every art slot's `asset`
    from its prompt/transparent diff regardless of what a revision wrote,
    so an edit here would be silently overwritten even if let through. It
    is refused here instead, loudly, so a model that tries shows up in
    RevisionResult.skipped rather than looking like it succeeded."""
    head = tokens[0]
    if head in ("version", "notes_log"):
        return True
    return head == "art" and "asset" in tokens[1:]


def _resolve_container(root: dict, tokens: list[str | int]
                       ) -> tuple[Any, str | int] | None:
    """Walk every token but the last, returning (the container it lands in,
    the final token) — or None the moment an intermediate step doesn't
    exist. The last token's own validity — an existing dict key, or a list
    index in [0, length] — is _apply_edit's job, since only it knows
    whether the edit is a replace or an append."""
    node: Any = root
    for token in tokens[:-1]:
        if isinstance(token, str):
            if not isinstance(node, dict) or token not in node:
                return None
            node = node[token]
        else:
            if not isinstance(node, list) or not (0 <= token < len(node)):
                return None
            node = node[token]
    return node, tokens[-1]


def _apply_edit(root: dict, edit: SpecEdit) -> str | None:
    """Apply one SpecEdit to `root` (a plain spec dict, mutated in place).

    Returns None on success, or a human-readable reason it was skipped: a
    path that doesn't parse, a guarded path, a value that isn't valid JSON,
    or a location that doesn't resolve (§6.2: "anything else unresolvable =
    invalid edit"). Every check here is STRUCTURAL — whether the value
    actually belongs at that location (a number where a hex string is
    required, say) is CoverSpec.model_validate's job, once every edit in
    the batch has been applied; that failure is a RevisionError, not a
    skip, because it means the edits as a whole don't add up to a valid
    spec rather than that one of several independent edits misfired."""
    tokens = _parse_path(edit.path)
    if tokens is None:
        return f"{edit.path!r}: not a valid path"
    if _is_guarded(tokens):
        return (f"{edit.path!r}: guarded — version, notes_log, and art "
                f"asset paths are code's alone to write")
    try:
        value = json.loads(edit.value)
    except json.JSONDecodeError:
        return f"{edit.path!r}: value {edit.value!r} is not valid JSON"

    resolved = _resolve_container(root, tokens)
    if resolved is None:
        return f"{edit.path!r}: does not resolve against the current spec"
    container, last = resolved
    if isinstance(last, str):
        if not isinstance(container, dict) or last not in container:
            return f"{edit.path!r}: no such field"
        container[last] = value
    else:
        if not isinstance(container, list) or not (0 <= last <= len(container)):
            return f"{edit.path!r}: list index out of range"
        if last == len(container):
            container.append(value)
        else:
            container[last] = value
    return None


def _revision_system_prompt() -> str:
    return """You are editing a book cover design document (a CoverSpec) by \
writing a small list of PATCH EDITS against it, not by rewriting the whole \
document. You will be given the current spec as JSON and a human's notes \
about what to change. Return only the edits the notes require — at most \
40 — and leave every field you don't mention exactly as it already is; \
there is nothing to copy through.

Each edit is a {path, value} pair. `path` is a dotted chain of field \
names with an optional `[n]` list index on any segment: `palette.primary` \
(a top-level field), `text[1].zone.y` (the second text slot's zone's y), \
`art[0].prompt` (the first art slot's prompt), `scrims[0].strength`. A \
bare list field name with no index — `layers` on its own — replaces the \
WHOLE list. A list index addresses a slot by its POSITION in the spec \
JSON you were given, never by its `id` — find which index holds the `id` \
you mean before writing its path.

`value` is the new value, JSON-encoded AS A STRING: quote a string value \
("#a83250"), leave a number bare (0.13), use JSON array syntax for a pair \
([0.5, 1.0]), use JSON object syntax for a whole nested object \
({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.15}).

Seven worked examples:
1. Move the title zone up: the spec JSON shows a text slot with "id": \
"title" at index 0, whose zone.y is currently 0.62. Edit: path \
`text[0].zone.y`, value `0.57`.
2. Recolor the palette: path `palette.primary`, value `"#a83250"`.
3. Resize the title's type: path `text[0].size_max`, value `0.13`.
4. The notes say "warmer and moodier": find the adjust layer whose op is \
"grade" (say index 0) and the one whose op is "vignette" (say index 1) — \
two edits: path `adjust[0].temperature`, value `0.35`; path \
`adjust[1].strength`, value `0.4`. (No grade layer to warm? Warming the \
palette hexes themselves is the fallback.)
5. The notes say the type feels pasted on: move the `fx_`-prefixed \
finishing layers ABOVE the text layers so grain and grade sit over \
everything at once — ONE edit replacing the whole z-order: path `layers`, \
value the entire reordered list as a JSON array of {"kind", "ref"} \
objects copied from the spec you were shown, with the fx_ entries moved \
after the text entries.
6. The notes say "make the title a stacked poster title": path \
`text[0].fit_mode`, value `"justify_stack"`.
7. The notes say "put the forest inside the title", and the forest art \
slot sits at index 1 with no mask yet: path `art[1].mask`, value \
`{"from_text": "title"}`. (When a slot ALREADY carries a mask object, \
edit the one field instead: path `art[1].mask.from_text`, value \
`"title"` — a path can only step INTO an object that exists in the spec \
you were shown.)

A path may replace any existing location. A list index equal to that \
list's CURRENT length appends a new element there; anything else — an \
out-of-range index, or a field name not already in the spec you were \
shown — is an invalid edit, skipped rather than guessed at.

You may: move or resize zones; change palette hexes; swap fonts (from \
the same closed list already used in the spec — nothing outside it is \
valid); change text case, tracking, or align; adjust scrim strengths and \
art transforms (scale, offset, anchor); adjust a text slot's mask_from \
(which art slot's shape it is clipped into) or a container art slot's \
own placement and scale; rewrite an art_prompt; toggle the texture layer \
on or off; edit an adjust layer's grade and strength fields \
(temperature, brightness, contrast, saturation, strength) and any \
`fx_`-prefixed finishing layer's opacity or blend; reorder `layers` as a \
whole-list replace; set or edit a layer's `mask` (a gradient, from_layer, \
from_text, luminance_of, or invert); and set a text slot's expressive-\
type fields (fit_mode, arc, rotate, emphasis, emphasis_style) where the \
spec carries them.

You may NOT, ever: write to `version` or `notes_log` — the calling code \
owns both, and any edit touching them is refused before it reaches the \
design. You may NOT touch an art slot's `asset` field — code alone \
decides which assets need to be regenerated, from whether you changed \
that slot's prompt or transparent, never from what you write here. You \
may NOT invent a new art, scrim, or text slot that was not in the input \
spec — wanting new art is out of scope for a revision; express it \
instead as a rewritten prompt or a new treatment on an EXISTING slot.

You additionally may not, unless the notes explicitly say so: change \
`archetype`; change a text slot's `content`.

Change only what the notes require, and nothing else."""


def _revision_user_prompt(spec: CoverSpec, notes: str) -> str:
    return (f"Current cover spec (JSON) — your edits' [n] indices address "
            f"positions in these same arrays:\n{spec.model_dump_json()}\n\n"
            f"Notes from a human editor — apply exactly these changes and "
            f"nothing else:\n{notes}")


def revise_spec(spec: CoverSpec, notes: str, provider: Provider, *,
                model: str = REVISION_MODEL) -> RevisionResult:
    """One structured call that edits a spec via a small list of patch
    edits, applied and validated in code (spec §6.2).

    The model answers with SpecEdits, never the document itself (see this
    section's header comment for why); each edit is applied to a COPY of
    `spec`'s own dict (`_apply_edit`), and the patched dict is then
    validated as a whole real CoverSpec — the same validation a fresh
    build_spec or the old full-echo reply went through. An edit that
    doesn't parse, resolves nowhere, or touches a guarded path (`version`,
    `notes_log`, an art slot's `asset`) is skipped rather than applied;
    every skip is named in the returned RevisionResult.skipped, which the
    caller is free to ignore.

    Raises RevisionError on any failure: a call error, a schema mismatch on
    the edits themselves, or edits that — once applied — produce a spec
    that CoverSpec itself rejects. This function never mutates `spec`, so the
    caller keeps the prior version on every failure path. On success, the
    returned spec's `version` is bumped and `notes` is appended to
    `notes_log` in code — never trusted from the model, which cannot even
    address either field (see `_is_guarded`) — and any art slot whose
    `prompt` or `transparent` changed has its `asset` cleared, which is the
    signal docproof.cover.pipeline.run_revision uses to regenerate exactly
    that one image and no other. Zero edits applied — an empty `edits` list,
    or every edit in it skipped — still runs this same bookkeeping and
    returns a spec identical in every OTHER field to the input; the
    pipeline's own _dump_equal_ignoring_bookkeeping is what turns that into
    a visible no-op rather than a wasted recompose."""
    usage = Usage()
    try:
        result = provider.complete_structured(
            model=model, system=_revision_system_prompt(),
            user=_revision_user_prompt(spec, notes),
            schema=strict_json_schema(SpecEdits),
            schema_name="cover_revision",
            max_tokens=MAX_OUTPUT_TOKENS)
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            raise RevisionError(
                f"The model did not return any revision edits: "
                f"{result.error or result.stop_reason}.")
        edits = SpecEdits.model_validate(result.parsed)
    except ValidationError as e:
        raise RevisionError(
            f"The model's revision edits did not match the schema: "
            f"{e}") from e
    except RevisionError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise RevisionError(f"The revision call failed: {e}") from e

    working = spec.model_dump(mode="json")
    skipped = tuple(reason for edit in edits.edits
                    if (reason := _apply_edit(working, edit)) is not None)
    try:
        revised = CoverSpec.model_validate(working)
    except ValidationError as e:
        raise RevisionError(
            f"The revised spec did not match the schema: {e}") from e

    old_art = {a.id: a for a in spec.art}
    new_art: list[ArtSlot] = []
    for slot in revised.art:
        old = old_art.get(slot.id)
        # A brand-new slot id (an edit breaking the "no new slots" rule)
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
                          cost=cost_of_usage(usage, fallback_model=model),
                          skipped=skipped)


__all__ = ["DIRECTION_MODEL", "REVISION_MODEL", "DirectionError",
          "DirectionResult", "RevisionError", "RevisionResult", "SpecEdit",
          "SpecEdits", "revise_spec", "run_directions"]
