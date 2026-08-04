"""What prep passes between its stages.

The review pipeline's `Finding` is an edit inside a paragraph; prep's unit is
the paragraph itself — what it is, what style it becomes, and whether it
survives. Nothing here knows a house style name: styles travel as strings that
came out of the style sheet.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StructureParagraph:
    """One `w:p`, including the blank ones. Blank lines are the author's own
    scene-break signal, which is why prep reads a document the review pipeline
    would have filtered down to a third of its size."""
    para_id: str
    part: str
    index: int              # position in document order — what the model reasons over
    location: str           # "body" | "table" | "header" | ...
    text: str               # paragraph_text(), unmodified
    style: str              # whatever the source called it, usually "Normal"
    is_blank: bool
    leading_ws: int         # typed spaces/tabs before the first word
    trailing_ws: int
    has_italics: bool
    has_link: bool
    is_list: bool = False   # a numbered or bulleted item

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass(frozen=True)
class Structure:
    """Every paragraph of the manuscript, in document order."""
    source_path: str
    paragraphs: tuple[StructureParagraph, ...]
    # Paragraphs prep reads but does not tag: headers, footers, notes. Recorded
    # so the notes can say they were left alone rather than silently ignoring
    # them.
    untouched: tuple[tuple[str, str], ...] = ()

    @property
    def taggable(self) -> tuple[StructureParagraph, ...]:
        return self.paragraphs

    @property
    def word_count(self) -> int:
        return sum(p.words for p in self.paragraphs)


@dataclass(frozen=True)
class Tag:
    """One paragraph as the model labelled it."""
    para_id: str
    role: str               # a style name from the sheet, or a pseudo-role
    flag: str = ""          # something the model wants the designer to see
    source: str = "model"   # "model" | "unanswered"


@dataclass(frozen=True)
class Flag:
    """Something a human has to decide. The spec is explicit that these are
    raised, never fixed quietly."""
    para_id: str
    kind: str
    message: str
    preview: str = ""


@dataclass(frozen=True)
class ParagraphPlan:
    """What the writers do to one paragraph. Both writers consume exactly this,
    which is what keeps the clean file and the tracked file the same decision
    expressed two ways."""
    para_id: str
    role: str                     # what the model said, kept for the notes
    style: str | None = None      # house style name to apply, None when dropped
    drop: bool = False            # a blank line that was only spacing
    insert_glyph: bool = False    # a blank line that was a scene break
    strip_leading: int = 0
    strip_trailing: int = 0

    @property
    def is_change(self) -> bool:
        return bool(self.drop or self.insert_glyph or self.style
                    or self.strip_leading or self.strip_trailing)


@dataclass(frozen=True)
class PrepPlan:
    plans: tuple[ParagraphPlan, ...]
    flags: tuple[Flag, ...]
    glyph: str                    # what an inserted scene break says

    def by_id(self) -> dict[str, ParagraphPlan]:
        return {p.para_id: p for p in self.plans}

    @property
    def style_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self.plans:
            if p.style:
                counts[p.style] = counts.get(p.style, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    @property
    def dropped(self) -> int:
        return sum(1 for p in self.plans if p.drop)

    @property
    def inserted_breaks(self) -> int:
        return sum(1 for p in self.plans if p.insert_glyph)

    @property
    def trimmed(self) -> int:
        return sum(1 for p in self.plans
                   if p.strip_leading or p.strip_trailing)
