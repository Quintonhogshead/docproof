"""The director: one model reads the WHOLE book and assigns each concept its
cover spec, plus the notes the agent executing it will need.

This replaces the old two-call opening (docproof.cover.reality distilling a
sample, then docproof.cover.direction.run_directions turning that sheet into
N concepts). The owner's verdict on the six hand-built Longsword covers was
that the useful thing a director does is not "invent a palette" — it is
*read the book*, decide what each cover is about, and then tell whoever
executes it which trap that particular design sets. A sample cannot do the
first thing and a schema alone cannot do the third.

Three decisions this module is built around:

- **The whole manuscript, held in memory, never written down.** The uploaded
  file is read into this call and dropped; only the assignments are
  persisted (docproof.cover.pipeline). Cover Studio has never stored a
  manuscript and still doesn't. `fit_manuscript` is the one concession to
  physics: past MAX_BOOK_WORDS the text is sliced rather than truncated, and
  the caller ledgers the fact so nobody believes a 200k-word novel was read
  end to end when it wasn't.
- **An assignment is a Direction plus a job description.** `execution_notes`
  is the concept's specific traps ("the cutout will come back with no
  ground; generate the tarmac and the cast shadow") and `done_when` is what
  finished looks like for THIS cover. Both are prose written for the agent
  in docproof.cover.atelier, which is a reader, not a parser.
- **Never guess.** DirectorError on any trouble, the same contract
  run_directions and distill_reality share: a junk assignment goes on to
  spend real image-generation dollars.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..models import Usage
from ..providers import Provider, cost_of_usage, strict_json_schema
from . import doctrine
from .archetypes import ARCHETYPES, describe_archetypes
from .fonts import describe_fonts
from .model import Brief, Direction
from .recipes import describe_recipes

log = logging.getLogger("docproof.cover.director")

# The frontier reasoner. Mirrored, not imported, from
# docproof.cover.direction.DIRECTION_MODEL — the same convention that module
# documents for mirroring docproof.quest.skin.LUNA_MODEL: same value, not a
# dependency, so either can be repinned without dragging the other.
DIRECTOR_MODEL = "claude-fable-5"

# Structured replies on a reasoning model share max_tokens with thinking, and
# a truncated structured reply parses as nothing (the house rule every model
# call in this package is built around). N assignments each carrying prose is
# the longest structured reply Cover Studio asks for, so this is generous.
MAX_OUTPUT_TOKENS = 16000

# How much book one director call reads. A 120k-word novel is roughly 160k
# tokens, which fits; past this the read is sliced across the book instead,
# because a truncated head is a worse brief than an honest set of slices.
MAX_BOOK_WORDS = 120_000

# How many evenly spaced slices an over-long book is read as, and how many
# words each carries. Openings and endings matter more than middles for cover
# purposes, so the first and last slices are kept whole by _slice_book.
BOOK_SLICES = 8


class DirectorError(RuntimeError):
    """The director call failed or answered with something unusable."""


class ConceptAssignment(BaseModel):
    """One concept: the design, plus what the agent building it must know."""
    model_config = ConfigDict(extra="forbid")

    direction: Direction
    execution_notes: str = Field(
        description="What this specific design will get wrong if the agent "
                    "is careless: the doctrine traps this archetype sets, "
                    "the clauses the art prompt must carry to pre-empt them, "
                    "and what to check in the render report.")
    done_when: str = Field(
        description="What finished looks like for THIS cover, concretely "
                    "enough that the agent can decide it is done without "
                    "asking. Name what must be true at thumbnail size.")


class Assignments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reading: str = Field(
        description="What this book is actually about, in the terms a cover "
                    "has to work in: the concrete objects, places and images "
                    "the prose returns to, the emotional register, and the "
                    "single thing a browser must understand in one second.")
    concepts: list[ConceptAssignment]


@dataclass(frozen=True)
class DirectorResult:
    assignments: list[ConceptAssignment]
    reading: str
    model: str
    cost: float | None
    words_read: int
    sliced: bool


def _slice_book(words: list[str]) -> str:
    """An over-long book as labelled slices across its whole length.

    Labelled, because an unlabelled concatenation reads as one continuous
    passage with baffling jumps in it, and a director that thinks the prose
    is incoherent will write incoherent briefs. The first and last slices are
    where a book states and answers itself, so they get double weight."""
    # BOOK_SLICES + 2 units, because the first and last slices are double.
    # The labels are words too, so they come out of the budget rather than
    # sitting on top of it — the ceiling has to be a real ceiling.
    per = (MAX_BOOK_WORDS - BOOK_SLICES * 4) // (BOOK_SLICES + 2)
    starts = [round(i * (len(words) - per) / (BOOK_SLICES - 1))
              for i in range(BOOK_SLICES)]
    parts: list[str] = []
    for i, start in enumerate(starts):
        take = per * 2 if i in (0, BOOK_SLICES - 1) else per
        if i == BOOK_SLICES - 1:
            # The last slice is anchored to the END of the book, not to its
            # own start: a double-weight slice starting at len-per would run
            # off the end and quietly come back single-weight, so the ending
            # would get exactly the weight this function says it doesn't.
            start = max(0, len(words) - take)
        chunk = " ".join(words[start:start + take])
        pct = round(100 * start / max(len(words), 1))
        if i == 0:
            label = "THE OPENING"
        elif i == BOOK_SLICES - 1:
            label = "THE ENDING"
        else:
            label = f"ABOUT {pct}% THROUGH"
        parts.append(f"[{label}]\n{chunk}")
    return "\n\n".join(parts)


def fit_manuscript(text: str) -> tuple[str, int, bool]:
    """The manuscript as the director will actually read it.

    Returns (text, words_in_the_book, was_sliced). A book inside the budget
    is passed through whole and unlabelled — the point of this module is that
    the director reads the book."""
    words = text.split()
    if len(words) <= MAX_BOOK_WORDS:
        return text, len(words), False
    return _slice_book(words), len(words), True


def _system_prompt(n: int, genre: str) -> str:
    plural = "concept" if n == 1 else "concepts"
    return f"""You are the art director for a literary press, and you have \
just read a manuscript end to end. Your job is to assign {n} cover \
{plural} — not to describe them vaguely, but to specify each one and then \
brief the designer who will build it.

Each concept is executed by an autonomous agent with the composer, the image \
generator, and a fixed budget. That agent has NOT read the book. Everything \
it knows about why this cover is right comes from you.

WHAT A COVER MUST DO. A browser sees a thumbnail for about one second. \
Decide what that second has to deliver for this book, say it in `reading`, \
and make every concept serve it. If the title itself misleads about the \
genre, killing that misreading is a concept's first job, and you should say \
so in its execution notes.

DISTINCTNESS. The {n} {plural} must differ from each other structurally — a \
different archetype, or a sharply different palette and image. {n} variations \
on one idea is a failed assignment.

GROUNDING. Every image you specify must come from the manuscript you just \
read: its objects, its places, its weather, its recurring gestures. Do not \
reach for the genre's stock furniture. Name real things from the book.

ART PROMPTS. The image generator produces ART LAYERS ONLY. Never ask for \
text, lettering, titles, signage copy, numbers, logos or brand marks in an \
art prompt — the composer sets all type itself. Write prompts as scenes with \
a stated medium, light direction and camera, not as lists of nouns.

EXECUTION NOTES. This is the half a schema cannot carry. For each concept, \
name the specific way THIS design fails if built carelessly, and the clause \
the art prompt must carry to pre-empt it. Be concrete and mechanical.

DONE WHEN. Give the agent a finish line it can judge itself against at \
thumbnail size, so it stops when the cover is right rather than when its \
budget runs out.

{doctrine.render("direction")}

THE ARCHETYPE SHELF — pick `archetype` from these exact names, and write art \
prompts only for the slot ids the chosen archetype lists:

{describe_archetypes(genre)}

THE FONT SHELF — `title_font` and `author_font` come from these exact names:

{describe_fonts()}

THE FINISHING RECIPES — `recipe` is one of these, or "" for none:

{describe_recipes()}"""


def _user_prompt(brief: Brief, manuscript: str, sliced: bool) -> str:
    lines = [
        "THE BOOK",
        f"Title: {brief.title}",
    ]
    if brief.subtitle:
        lines.append(f"Subtitle: {brief.subtitle}")
    lines.append(f"Author: {brief.author}")
    lines.append(f"Genre: {brief.genre}")
    if brief.pitch:
        lines.append(f"Pitch: {brief.pitch}")
    if brief.mood:
        lines.append(f"Mood: {brief.mood}")
    if brief.must_include:
        lines.append(f"Must include: {brief.must_include}")
    if brief.avoid:
        lines.append(f"Avoid: {brief.avoid}")
    if manuscript:
        note = ("The manuscript is long, so it is given as labelled slices "
                "across its whole length rather than continuously."
                if sliced else
                "The complete manuscript follows. Read all of it.")
        lines += ["", "THE MANUSCRIPT", note, "", manuscript]
    else:
        lines += ["", "No manuscript was supplied. Work from the brief alone, "
                  "and say so in `reading`."]
    return "\n".join(lines)


def _validate(assignment: ConceptAssignment) -> ConceptAssignment:
    """Same tolerance run_directions settled on: a fabricated archetype is
    fatal because nothing downstream can render it, but an art prompt for a
    slot this archetype does not generate is dropped with a log line rather
    than killing the other concepts in a paid job."""
    d = assignment.direction
    archetype = ARCHETYPES.get(d.archetype)
    if archetype is None:
        raise DirectorError(
            f"{d.concept_name!r} picked archetype {d.archetype!r}, which is "
            f"not one of the shipped archetypes "
            f"({', '.join(sorted(ARCHETYPES))}).")
    generatable = {a.id for a in archetype.art if a.generatable}
    extra = sorted({p.slot for p in d.art_prompts} - generatable)
    if not extra:
        return assignment
    log.info("Assignment %r wrote art prompts for %s, which the %s archetype "
             "does not generate; dropped.", d.concept_name, ", ".join(extra),
             archetype.name)
    kept = [p for p in d.art_prompts if p.slot in generatable]
    return assignment.model_copy(
        update={"direction": d.model_copy(update={"art_prompts": kept})})


def assign_concepts(brief: Brief, provider: Provider, *, n: int,
                    manuscript: str = "",
                    model: str = DIRECTOR_MODEL) -> DirectorResult:
    """One structured call: the whole book in, n assignments out.

    Raises DirectorError on any failure — a call error, a schema mismatch, a
    concept count that doesn't match `n`, or a concept naming an archetype
    that doesn't exist. There is no fallback, for run_directions' own reason:
    a junk assignment goes on to spend real image-generation dollars."""
    text, words, sliced = fit_manuscript(manuscript)
    genre = brief.genre.strip().lower().replace(" ", "_")
    usage = Usage()
    try:
        result = provider.complete_structured(
            model=model,
            system=_system_prompt(n, genre),
            user=_user_prompt(brief, text, sliced),
            schema=strict_json_schema(Assignments),
            schema_name="cover_assignments",
            max_tokens=MAX_OUTPUT_TOKENS)
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            raise DirectorError(
                f"The director did not return any cover assignments: "
                f"{result.error or result.stop_reason}.")
        assignments = Assignments.model_validate(result.parsed)
    except ValidationError as e:
        raise DirectorError(
            f"The director's assignments did not match the schema: {e}") from e
    except DirectorError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise DirectorError(f"The director call failed: {e}") from e

    if len(assignments.concepts) != n:
        raise DirectorError(
            f"Asked the director for {n} cover concepts but got "
            f"{len(assignments.concepts)}.")

    return DirectorResult(
        assignments=[_validate(a) for a in assignments.concepts],
        reading=assignments.reading, model=model,
        cost=cost_of_usage(usage, fallback_model=model),
        words_read=words, sliced=sliced)


__all__ = ["BOOK_SLICES", "DIRECTOR_MODEL", "MAX_BOOK_WORDS",
           "MAX_OUTPUT_TOKENS", "Assignments", "ConceptAssignment",
           "DirectorError", "DirectorResult", "assign_concepts",
           "fit_manuscript"]
