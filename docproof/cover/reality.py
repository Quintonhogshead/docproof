"""The reality sheet: distilling a manuscript sample into a compact,
structured brief of concrete grounding facts, so the art-direction call
(docproof.cover.direction.run_directions) reads a curated few hundred words
instead of a raw manuscript excerpt (the BRAIN wave's manuscript-grounding
decision, 2026-08-29 — the owner's beta verdict was that a raw sample lets
the direction call get overwhelmed or distracted by whatever the opening
happens to dwell on, rather than reliably grounding imagery in the book's
actual concrete texture).

One structured call, same shape as every other model call in this package
(docproof.cover.direction.run_directions/revise_spec, docproof.quest.skin.
generate_skin): a Provider, strict_json_schema, a generous MAX_OUTPUT_TOKENS
because a truncated structured reply parses as nothing, cost_of_usage for
pricing. REALITY_MODEL is mirrored rather than imported from direction.py's
own constants on purpose — same convention, not a dependency, the reasoning
direction.py's own module docstring already gives for mirroring
docproof.quest.skin.LUNA_MODEL.

Failure posture is the ONE deliberate difference from run_directions/
revise_spec: distill_reality raises RealitySheetError on any trouble, the
same "never guess" contract every other call in this package shares, but
UNLIKE a bad direction or a bad revision, a bad reality sheet is not worth
stopping a job over — the caller (docproof.cover.pipeline.run_job) catches
it and falls back to the raw manuscript sample, logging and ledgering the
fallback rather than ever blocking on it (a distillation failure just means
the direction call sees prose instead of a curated sheet, which is exactly
what every job did before this module existed). That policy lives at the
call site, not here, the same way docproof.cover.critique's own module
docstring draws the identical line for CritiqueError.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..models import Usage
from ..providers import Provider, cost_of_usage, strict_json_schema

log = logging.getLogger("docproof.cover.reality")

# The workhorse model — distillation is a careful-extraction task, not a
# taste call, the same tier docproof.cover.direction.REVISION_MODEL is
# priced for. A real docproof.providers.catalog id.
REALITY_MODEL = "claude-sonnet-5"

# Structured replies on a reasoning model share max_tokens with thinking, and
# a truncated structured reply parses as nothing — so leave far more room
# than a RealitySheet needs (see docproof.cover.direction's identical
# comment on its own MAX_OUTPUT_TOKENS).
MAX_OUTPUT_TOKENS = 8000

# The rendered sheet's own word ceiling (docs: "~300-word ceiling on the
# rendered sheet") — enforced in code by render_reality_sheet, not merely
# requested of the model, so it is a real guarantee rather than a hope.
RENDERED_WORD_CEILING = 300


class RealitySheetError(RuntimeError):
    """The distillation call failed outright, or came back unusable. Always
    carries a sentence a person could read, the same convention as
    docproof.cover.direction.DirectionError. docproof.cover.pipeline is what
    decides this is non-fatal (falls back to the raw sample) — that policy
    lives at the call site, not here; see the module docstring."""


class RealitySheet(BaseModel):
    """What one manuscript sample distills down to: concrete grounding facts
    only, never invented detail — see _system_prompt for the extraction
    rules. Every field is required on the wire (the same "a field with a
    Python default is still a required key on the wire" convention
    strict_json_schema documents) even though defaults are supplied here so
    a hand-built RealitySheet is convenient in tests."""
    model_config = ConfigDict(extra="forbid")

    setting: str = ""
    era: str = ""
    palette_cues: list[str] = Field(default_factory=list)
    concrete_objects: list[str] = Field(default_factory=list, max_length=8)
    motifs: list[str] = Field(default_factory=list, max_length=6)
    atmosphere: str = ""
    never_show: list[str] = Field(default_factory=list, max_length=4)


@dataclass(frozen=True)
class RealityResult:
    """One distillation call's answer, plus what it cost. `rendered` is the
    plain-text block docproof.cover.pipeline.run_job hands to run_directions
    AS its manuscript_sample argument — see render_reality_sheet."""
    sheet: RealitySheet
    rendered: str
    model: str
    cost: float | None


def _system_prompt() -> str:
    return """You are a manuscript reader distilling ONE sample from a novel \
into a compact REALITY SHEET for a book-cover art director who will never \
read the manuscript itself — only your sheet. Extract only what is \
concretely THERE in the sample; never invent a detail the text does not \
support, and never pad a field just to fill it.

- setting: one short phrase naming the concrete place(s) the sample is set.
- era: one short phrase naming the time period or historical era (or \
"contemporary" if the text reads as present-day).
- palette_cues: a small handful of short phrases naming colors, materials, \
or light the text itself evokes (e.g. "brass and soot", "moonlit snow on \
canvas", "sun-bleached rope") — grounded in what the prose actually \
describes, not a color you would merely associate with the genre.
- concrete_objects: up to 8 concrete, nameable objects, places, or props \
that actually appear in the sample (a lighthouse, a pocket watch, a \
rowboat) — never abstractions like "hope" or "danger".
- motifs: up to 6 short phrases naming recurring images or symbols the text \
itself returns to more than once.
- atmosphere: one or two sentences on the sample's mood and register — the \
feeling a cover for this specific book should carry.
- never_show: up to 4 short phrases naming anything a cover for this book \
must NOT depict — a plot spoiler, a specific violent or explicit image, a \
named real person, or any detail the text makes clear would misrepresent \
the book.

Ground every field in the sample's own text. If the sample gives you \
nothing supportable for a list field, leave that field empty rather than \
inventing content to fill it."""


def _user_prompt(manuscript_sample: str) -> str:
    return f"MANUSCRIPT SAMPLE:\n{manuscript_sample}"


def _cap_words(text: str, limit: int) -> str:
    """`text`, unchanged, when it is already at or under `limit` words —
    preserving its line breaks and labels exactly. Only when it runs over
    does this collapse whitespace (a truncated stream of words, marked with
    an ellipsis) — a rare path, since render_reality_sheet's own field caps
    keep the common case comfortably under the ceiling without ever
    reaching this branch."""
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]) + " …"


def render_reality_sheet(sheet: RealitySheet) -> str:
    """The distilled sheet as the plain-text block docproof.cover.pipeline.
    run_job hands to run_directions AS its manuscript_sample argument (same
    parameter — see docproof.cover.direction._sample_rule's docstring) —
    labeled sections, capped at RENDERED_WORD_CEILING words so a chatty
    model answer can never balloon back into something as long as the raw
    sample it was built to replace."""
    lines = []
    if sheet.setting:
        lines.append(f"Setting: {sheet.setting}")
    if sheet.era:
        lines.append(f"Era: {sheet.era}")
    if sheet.palette_cues:
        lines.append(f"Palette cues: {', '.join(sheet.palette_cues)}")
    if sheet.concrete_objects:
        lines.append(f"Concrete objects: {', '.join(sheet.concrete_objects)}")
    if sheet.motifs:
        lines.append(f"Motifs: {', '.join(sheet.motifs)}")
    if sheet.atmosphere:
        lines.append(f"Atmosphere: {sheet.atmosphere}")
    if sheet.never_show:
        lines.append(f"Never show: {', '.join(sheet.never_show)}")
    return _cap_words("\n".join(lines), RENDERED_WORD_CEILING)


def distill_reality(manuscript_sample: str, provider: Provider, *,
                    model: str = REALITY_MODEL) -> RealityResult:
    """One structured call: a manuscript sample becomes a RealitySheet, plus
    its rendered plain-text form (the thing run_directions actually reads).

    Raises RealitySheetError on any failure — a call error, a schema
    mismatch, an unusable reply. There is no fallback INSIDE this function
    (contrast with docproof.quest.skin.generate_skin, which degrades to
    DEFAULT_SKIN itself); the fallback to the raw sample is the caller's
    call (docproof.cover.pipeline.run_job), the same division
    docproof.cover.critique draws for CritiqueError — see the module
    docstring."""
    usage = Usage()
    try:
        result = provider.complete_structured(
            model=model, system=_system_prompt(),
            user=_user_prompt(manuscript_sample),
            schema=strict_json_schema(RealitySheet),
            schema_name="cover_reality_sheet",
            max_tokens=MAX_OUTPUT_TOKENS)
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            raise RealitySheetError(
                f"The model did not return a reality sheet: "
                f"{result.error or result.stop_reason}.")
        sheet = RealitySheet.model_validate(result.parsed)
    except ValidationError as e:
        raise RealitySheetError(
            f"The reality sheet did not match the schema: {e}") from e
    except RealitySheetError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK/network variants
        raise RealitySheetError(f"The reality-sheet call failed: {e}") from e

    rendered = render_reality_sheet(sheet)
    return RealityResult(sheet=sheet, rendered=rendered, model=model,
                         cost=cost_of_usage(usage, fallback_model=model))


__all__ = ["MAX_OUTPUT_TOKENS", "REALITY_MODEL", "RENDERED_WORD_CEILING",
          "RealityResult", "RealitySheet", "RealitySheetError",
          "distill_reality", "render_reality_sheet"]
