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
    index: int
    location: str
    text: str
    style: str
    is_blank: bool
    leading_ws: int
    trailing_ws: int
    has_italics: bool
    has_link: bool
    has_image: bool = False
    is_list: bool = False
    # A line of the manuscript's own table of contents. Read from the file
    # rather than from the words, because "Chapter One" in a contents list and
    # "Chapter One" forty pages later are the same eleven characters.
    is_toc: bool = False

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def preserved(self) -> bool:
        """Left exactly as it arrived: everything inside a table, and an image
        sitting on its own line. A table's layout and an image's placement are
        the author's, not running text to restyle — so these paragraphs are
        never tagged, never restripped, never dropped as blank. (A prose
        paragraph that happens to hold an inline image is still prose; the
        image element itself is untouched by every writer.)"""
        return self.location == "table" or (
            self.has_image and not self.text.strip())


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
        """What the model is asked about — and, downstream, the only
        paragraphs the plan and the writers will touch. Preserved paragraphs
        (tables, image lines) stay in `paragraphs`, so the word-for-word check
        still answers for their text, but no label is ever bought for them and
        no writer restyles them."""
        return tuple(p for p in self.paragraphs if not p.preserved)

    @property
    def word_count(self) -> int:
        return sum(p.words for p in self.paragraphs)


@dataclass(frozen=True)
class Tag:
    """One paragraph as the model labelled it."""
    para_id: str
    role: str
    flag: str = ""
    source: str = "model"


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
    role: str
    style: str | None = None
    drop: bool = False
    insert_glyph: bool = False
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
    glyph: str

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
