"""What the manuscript is: subject, title, author — asked once, answered small.

The book output needs three facts the tagger never asks for: what the book is
about (that picks the display face), its title and its author (they sit on the
running heads). One structured call over the opening paragraphs answers all
three. The subject answer is constrained to the design's own subject keys, so
a face that doesn't exist cannot be picked on the wire; the operator can
override any of the three per job, and an override skips nothing but its own
question — the model still fills whatever was left blank.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import ValidationError, create_model

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .book_design import BookDesign, BookMeta
from .chunker import preview
from .model import Structure

log = logging.getLogger("docproof.prep.meta")

# The opening pages are enough: title pages, bylines and the first chapter all
# sit at the front, and a subject readable at all is readable there.
OPENING_PARAGRAPHS = 60
PREVIEW_CHARS = 240
MAX_OUTPUT_TOKENS = 300

_SYSTEM = """\
You are cataloguing a book manuscript for its interior design. From the \
opening paragraphs, report:

- title: the book's title exactly as the manuscript's own title page gives \
it, in its capitalization. Empty string if no title appears.
- author: the author's name exactly as the byline gives it. Empty string if \
no byline appears.
- subject: what kind of book this is, as exactly one of these keys, judged \
from the writing itself:

{subjects}

The file name may help with the title or author, but never with the subject."""


def detect_meta(structure: Structure, design: BookDesign, provider: Provider,
                *, model: str, usage: Usage,
                source_name: str = "") -> BookMeta:
    """One call, three facts. A failed or malformed answer degrades to an
    undetected BookMeta rather than failing the run — the book file is worth
    shipping with a default face and a file-name title."""
    opening = [p for p in structure.taggable if not p.is_blank]
    opening = opening[:OPENING_PARAGRAPHS]
    if not opening:
        return BookMeta()
    lines = [preview(p, PREVIEW_CHARS) for p in opening]
    user = "\n".join(filter(None, lines))
    if source_name:
        user = f'(file name: "{source_name}")\n{user}'

    facts_model = create_model(
        "BookFacts",
        title=(str, ...), author=(str, ...),
        subject=(Literal[design.subject_choices], ...))
    result = provider.complete_structured(
        model=model,
        system=_SYSTEM.format(subjects=design.describe_subjects()),
        user=user,
        schema=strict_json_schema(facts_model),
        schema_name="book_facts",
        max_tokens=MAX_OUTPUT_TOKENS)
    usage.add(result.usage)
    if result.stop_reason != "ok" or result.parsed is None:
        log.error("Subject detection failed: %s",
                  result.error or result.stop_reason)
        return BookMeta()
    try:
        facts = facts_model.model_validate(result.parsed)
    except ValidationError as e:
        log.error("Subject detection came back malformed: %s", e)
        return BookMeta()
    meta = BookMeta(subject=facts.subject, title=facts.title.strip(),
                    author=facts.author.strip(), detected=True)
    log.info("Detected: subject '%s', title %r, author %r.",
             meta.subject, meta.title, meta.author)
    return meta


def merge_meta(detected: BookMeta, *, subject: str = "", title: str = "",
               author: str = "") -> BookMeta:
    """The operator's answers over the model's, field by field. Empty string
    means 'use what was detected' — the same sentinel the judge and continuity
    prompt overrides use."""
    return BookMeta(
        subject=(subject or detected.subject).strip(),
        title=(title or detected.title).strip(),
        author=(author or detected.author).strip(),
        detected=detected.detected)
