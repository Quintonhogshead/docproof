"""The IDML package wrapper and the shared story walker.

This is the IDML counterpart of utils/xml_helpers.py, and it keeps the same
promise: ingest, the validator and the reassembler all derive paragraph text
from one function, so a validated offset means the same thing at both ends.

The shapes below were verified against real InDesign 2026 output — see
docs/idml-notes.md, which is the reference for every element name here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

from lxml import etree

from ...utils.package import ZipPackage

IDPKG_NS = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"

DESIGNMAP = "designmap.xml"
MIMETYPE = "mimetype"
IDML_MIMETYPE = b"application/vnd.adobe.indesign-idml-package"

STORY = "Story"
PSR = "ParagraphStyleRange"
CSR = "CharacterStyleRange"
CONTENT = "Content"
BR = "Br"
CHANGE = "Change"
NOTE = "Note"
TABLE = "Table"
CELL = "Cell"
FOOTNOTE = "Footnote"
DOCUMENT_USER = "DocumentUser"

INSERTED = "InsertedText"
DELETED = "DeletedText"

# Wrappers that carry no text of their own — walk straight through them.
_TRANSPARENT = {CSR, "HyperlinkTextSource", "HyperlinkTextDestination",
                "XMLElement", "Hyperlink"}
# Text that belongs to a container of its own, walked separately with its own
# paragraph ids.
_NESTED = {TABLE, FOOTNOTE}


class IdmlPackage(ZipPackage):
    """An .idml opened as a zip held in memory.

    Only `mimetype` needs special care and ZipPackage already gives it: entry
    order and per-entry compression are preserved, so `mimetype` stays first
    and stored."""

    def story_parts(self) -> list[str]:
        """Story part names in designmap order — the deterministic walk order.

        Taken from the designmap's <idPkg:Story src="…"/> children rather than
        from a glob of Stories/, because the designmap is what InDesign itself
        treats as the document, and its order never depends on the filesystem.
        The backing story (unplaced XML content) has no such entry and is
        deliberately left out."""
        if not self.has(DESIGNMAP):
            return []
        out = []
        for el in self.tree(DESIGNMAP).iterchildren(f"{{{IDPKG_NS}}}{STORY}"):
            src = el.get("src")
            if src and self.has(src):
                out.append(src)
        return out

    def story(self, part: str) -> etree._Element | None:
        return self.tree(part).find(STORY)


# --- the canonical text contract ---------------------------------------------

@dataclass(frozen=True)
class WalkedParagraph:
    """One paragraph. Unlike a docx w:p there is no single element to point at:
    a paragraph is a *span*, so it carries the ordered Content nodes that make
    it up plus the ParagraphStyleRange it belongs to."""
    part: str
    para_id: str
    location: str          # "body" | "table" | "footnote"
    style: str
    psr: etree._Element
    contents: tuple[etree._Element, ...]


def paragraph_text(contents) -> str:
    """THE anchoring contract. Every stage that reads paragraph text calls
    this, on the same Content nodes, in the same order."""
    return "".join(c.text or "" for c in contents)


def _stream(node) -> Iterator[tuple[str, etree._Element]]:
    """Walk one ParagraphStyleRange (or Cell/Footnote body), yielding
    ("content" | "break" | "nested", element) in document order.

    What this deliberately does NOT do is descend into unrecognised elements.
    An anchored text frame sits inside the paragraph's XML but is a separate
    story on the page; pulling its words into this paragraph's canonical text
    would corrupt every offset after it."""
    for child in node:
        tag = child.tag
        if not isinstance(tag, str):          # comments, processing instructions
            continue
        if tag == CONTENT:
            yield ("content", child)
        elif tag == BR:
            yield ("break", child)
        elif tag in _NESTED:
            yield ("nested", child)
        elif tag == CHANGE:
            # The accepted view: insertions count as text, deletions do not.
            if child.get("ChangeType") == INSERTED:
                yield from _stream(child)
        elif tag == NOTE:
            continue                          # margin comments are not prose
        elif tag in _TRANSPARENT:
            yield from _stream(child)
        # anything else (Tab, Rule, anchored objects…) contributes nothing


def _segments(psr) -> list[list[tuple[str, etree._Element]]]:
    """Split a ParagraphStyleRange's stream into paragraphs at <Br/>.

    A Br terminates a paragraph rather than separating two, so a PSR that ends
    with one does not imply a trailing empty paragraph. An empty segment in the
    *middle* is a genuinely blank paragraph and is kept, so paragraph numbering
    matches what InDesign shows."""
    segments: list[list[tuple[str, etree._Element]]] = [[]]
    for event in _stream(psr):
        if event[0] == "break":
            segments.append([])
        else:
            segments[-1].append(event)
    if len(segments) > 1 and not segments[-1]:
        segments.pop()
    return segments


def style_of(psr) -> str:
    """"ParagraphStyle/Chapter Head" -> "Chapter Head". The $ID/ prefix marks
    one of InDesign's built-in localized names and is dropped so skip patterns
    are written against what the user sees in the Styles panel."""
    raw = psr.get("AppliedParagraphStyle") or ""
    name = raw.split("/", 1)[1] if "/" in raw else raw
    if name.startswith("$ID/"):
        name = name[4:]
    return unquote(name) or "Normal"


def _cell_key(cell) -> tuple[int, int]:
    """(row, column) from Cell@Name, which is written "column:row".

    Document order already happens to be reading order, but sorting on the
    parsed coordinates means a file that disagrees still walks predictably."""
    try:
        col, row = (cell.get("Name") or "0:0").split(":", 1)
        return (int(row), int(col))
    except (ValueError, AttributeError):
        return (0, 0)


def _walk_container(container, part: str, location: str, prefix: str
                    ) -> Iterator[WalkedParagraph]:
    """Walk something whose ParagraphStyleRange children are paragraphs: a
    Story, a table Cell, or a Footnote body."""
    counters = {"p": 0, "tbl": 0, "fn": 0}
    for psr in container:
        if psr.tag != PSR:
            continue
        style = style_of(psr)
        for segment in _segments(psr):
            index = counters["p"]
            counters["p"] += 1
            yield WalkedParagraph(
                part=part, para_id=f"{prefix}p{index:04d}", location=location,
                style=style, psr=psr,
                contents=tuple(el for kind, el in segment if kind == "content"))
            # Tables and footnotes sit inside the paragraph that anchors them;
            # emitting them straight after it keeps the walk in reading order.
            for kind, el in segment:
                if kind != "nested":
                    continue
                if el.tag == TABLE:
                    k = counters["tbl"]
                    counters["tbl"] += 1
                    yield from _walk_table(el, part, f"{prefix}tbl{k}-")
                else:
                    k = counters["fn"]
                    counters["fn"] += 1
                    yield from _walk_container(el, part, "footnote",
                                               f"{prefix}fn{k}-")


def _walk_table(table, part: str, prefix: str) -> Iterator[WalkedParagraph]:
    for cell in sorted(table.findall(CELL), key=_cell_key):
        row, col = _cell_key(cell)
        yield from _walk_container(cell, part, "table",
                                   f"{prefix}r{row}-c{col}-")


def walk_package(pkg: IdmlPackage) -> Iterator[WalkedParagraph]:
    """Every reviewable paragraph, in deterministic document order. Called by
    ingest AND by the reassembler, on the same trees."""
    for part in pkg.story_parts():
        story = pkg.story(part)
        if story is None:
            continue
        story_id = story.get("Self") or Path(part).stem
        yield from _walk_container(story, part, "body", f"story-{story_id}-")
