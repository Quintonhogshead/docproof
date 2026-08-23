"""Reusable, targeted context assembly for examination sites."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import DocumentModel, ParagraphRef, index_paragraphs
from .site_models import ExaminationSite


_SENTENCE_END = re.compile(r"[.!?][\"'”’\)\]]*\s+")

# Recipes ContextService can actually assemble today. A candidate requesting
# anything outside this set has insufficient context and must not be screened as
# if it were fully contextualized (P3-02).
SUPPORTED_RECIPES = frozenset({
    "current sentence", "current paragraph", "previous paragraph",
    "next paragraph", "heading hierarchy",
})


def unsupported_recipes(context_recipe) -> tuple[str, ...]:
    """The requested recipe items ContextService cannot yet provide."""
    return tuple(r for r in context_recipe if r not in SUPPORTED_RECIPES)


@dataclass(frozen=True)
class SiteContext:
    context_id: str
    text: str
    para_ids: tuple[str, ...]
    recipes: tuple[str, ...]


class ContextService:
    """Build and cache only the slices a site's recipe requests.

    Phase one supports paragraph neighborhoods, current sentences, and heading
    hierarchy using the existing DocumentModel.  Recipes requiring entities,
    tables, rendered pages, or geometry are left explicit in the returned text
    instead of quietly substituting the whole manuscript.
    """

    def __init__(self, doc: DocumentModel):
        self.doc = doc
        self._by_id = index_paragraphs(doc)
        self._ordered = list(doc.paragraphs)
        self._positions = {p.para_id: i for i, p in enumerate(self._ordered)}
        self._cache: dict[tuple, SiteContext] = {}

    def for_site(self, site: ExaminationSite) -> SiteContext:
        anchor = site.anchors[0]
        key = (anchor.paragraph_id, anchor.start_offset, anchor.end_offset,
               site.context_recipe)
        if key in self._cache:
            return self._cache[key]
        para = self._by_id.get(anchor.paragraph_id or "")
        if para is None:
            context = SiteContext(
                _context_id(key), "[No paragraph context is available.]", (),
                site.context_recipe)
            self._cache[key] = context
            return context

        selected: list[ParagraphRef] = []
        blocks: list[str] = []
        for recipe in site.context_recipe:
            if recipe == "current sentence":
                blocks.append("CURRENT SENTENCE\n" + self._sentence(
                    para.text, anchor.start_offset, anchor.end_offset))
            elif recipe == "current paragraph":
                selected.append(para)
                blocks.append(f"CURRENT PARAGRAPH [{para.para_id}]\n{para.text}")
            elif recipe in {"previous paragraph", "next paragraph"}:
                offset = -1 if recipe.startswith("previous") else 1
                neighbor = self._neighbor(para.para_id, offset)
                if neighbor is not None:
                    selected.append(neighbor)
                    blocks.append(
                        f"{recipe.upper()} [{neighbor.para_id}]\n{neighbor.text}")
            elif recipe == "heading hierarchy":
                headings = self._headings_before(para.para_id)
                selected.extend(headings)
                blocks.append("HEADING HIERARCHY\n" + (
                    "\n".join(f"[{p.style}] {p.text}" for p in headings)
                    or "[No preceding heading is represented.]"))
            else:
                blocks.append(
                    f"[{recipe}: unavailable until the corresponding "
                    f"document-IR/semantic/rendering phase]")
        para_ids = tuple(dict.fromkeys(p.para_id for p in selected))
        text = "\n\n".join(blocks)
        # Identity follows the actual reusable slice, not the examined offsets.
        # Two sites in one sentence/paragraph therefore reference one context
        # block in a packet even though their anchors differ.
        context_id = _context_id((site.context_recipe, para_ids, text))
        context = SiteContext(context_id, text, para_ids, site.context_recipe)
        self._cache[key] = context
        return context

    def _neighbor(self, para_id: str, offset: int) -> ParagraphRef | None:
        pos = self._positions[para_id] + offset
        return self._ordered[pos] if 0 <= pos < len(self._ordered) else None

    def _headings_before(self, para_id: str) -> list[ParagraphRef]:
        pos = self._positions[para_id]
        found: list[ParagraphRef] = []
        for para in reversed(self._ordered[:pos + 1]):
            if para.style.lower().startswith("heading"):
                found.append(para)
                if len(found) >= 3:
                    break
        return list(reversed(found))

    @staticmethod
    def _sentence(text: str, start: int | None, end: int | None) -> str:
        if start is None:
            return text
        left = 0
        for match in _SENTENCE_END.finditer(text, 0, start):
            left = match.end()
        right_match = _SENTENCE_END.search(text, end if end is not None else start)
        right = right_match.end() if right_match else len(text)
        return text[left:right].strip() or text


def _context_id(key: tuple) -> str:
    import hashlib
    return "CTX-" + hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:16]
