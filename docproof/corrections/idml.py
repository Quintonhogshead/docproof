"""An IDML story as editable, paragraph-addressed text.

InDesign stores a story's text as a run of `<Content>` elements inside
`<CharacterStyleRange>`s inside `<ParagraphStyleRange>`s, with `<Br/>` marking
each paragraph break. To change a word you have to find which Content node(s)
carry it and edit them in place, which is what this module does — the IDML
sibling of the review pipeline's `reassembler`.

The unit of address is the paragraph, and no edit is ever allowed to reach
across one: a paragraph's text is the concatenation of its Content nodes and
contains no break, so replacing a span within it cannot swallow a paragraph
mark. That single rule is what makes the paragraph-merge failure the
hand-written scripts produced impossible here rather than merely unlikely.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import lxml.etree as ET

# Element tag localnames (the story parts are not namespaced; only the root is).
CONTENT = "Content"
BR = "Br"
PSR = "ParagraphStyleRange"
# Containers that hold their own nested paragraph ranges — a table's cells, a
# footnote's text. Their Content belongs to those inner ranges, not to the range
# that encloses the container, so the walk prunes them and reaches them on their
# own turn.
NESTED = {PSR, "Table", "Footnote", "Cell", "Note", "XmlStory"}
STORY_GLOB = "Stories/Story_"
BACKING_STORY = "XML/BackingStory.xml"
CSR = "CharacterStyleRange"
# Children of a CharacterStyleRange that occupy a position in the text flow. When a
# range is split around a span these are sliced by index and never duplicated —
# duplicating one would clone a footnote or a paragraph break. Everything else a
# range holds (`Properties`, and the applied-font element inside it) describes the
# range rather than sitting in it, and so is copied into every piece of the split;
# without that copy each new range would silently lose its font.
FLOW = {CONTENT, BR} | {"Table", "Footnote", "Note", "XmlStory", "Cell"}


@dataclass
class Paragraph:
    """One paragraph of a story: the Content elements that carry its text, in
    order, and the applied paragraph style. Offsets in `text` index the
    concatenation of the Content nodes."""
    index: int
    style: str
    nodes: list[ET._Element] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join((n.text or "") for n in self.nodes)

    def restyle(self, start: int, end: int, attrs: dict[str, str]) -> bool:
        """Apply character attributes (`{"FontStyle": "Italic"}`) to the
        `[start, end)` span of this paragraph, changing no text.

        This is how "italicize this" becomes a real edit rather than a note passed
        to a designer. InDesign carries local character formatting on the
        `CharacterStyleRange` that encloses the text, so styling part of a run means
        splitting that range into up to three — before, the span, after — with the
        attribute on the middle one. The pieces keep the original range's own
        attributes and properties, and its flow children stay in their original
        order, so a range that spans several paragraphs (one `CharacterStyleRange`
        commonly holds `Content Br Content Br …`) comes apart without disturbing a
        single break.

        Returns False, changing nothing, when the span cannot be styled cleanly —
        its text is not held directly by a character range, for instance. The
        caller flags that rather than writing something half-done."""
        if start < 0 or end > len(self.text) or start >= end:
            raise ValueError(f"span [{start}, {end}) outside paragraph "
                             f"of length {len(self.text)}")
        covered = self._split_out(start, end)
        if covered is None:
            return False
        # Group the covered nodes by the range that holds them: a span can run over
        # a range boundary (the reviewer marked across a word that was already
        # styled), and each range is then split on its own.
        groups: list[tuple[ET._Element, list[ET._Element]]] = []
        for node in covered:
            parent = node.getparent()
            if parent is None or parent.tag != CSR:
                return False
            if groups and groups[-1][0] is parent:
                groups[-1][1].append(node)
            else:
                groups.append((parent, [node]))
        for parent, nodes in groups:
            _style_range(parent, nodes, attrs)
        return True

    def _split_out(self, start: int, end: int) -> list[ET._Element] | None:
        """The Content nodes covering `[start, end)`, split at both ends so the span
        is exactly a whole run of them. None when the span cannot be isolated.

        Three separate passes rather than one clever one: each recomputes the
        paragraph offsets from the current node list, so a split made by an earlier
        pass cannot leave a later one reading stale coordinates."""
        if not self._split_at(start) or not self._split_at(end):
            return None
        covered: list[ET._Element] = []
        offset = 0
        for node in self.nodes:
            text = node.text or ""
            n0, n1 = offset, offset + len(text)
            offset = n1
            if text and n0 >= start and n1 <= end:
                covered.append(node)
        return covered or None

    def _split_at(self, pos: int) -> bool:
        """Ensure a Content-node boundary falls at paragraph offset `pos`, splitting
        the node that straddles it. True when a boundary is there (or already was)."""
        offset = 0
        for i, node in enumerate(self.nodes):
            text = node.text or ""
            n0, n1 = offset, offset + len(text)
            offset = n1
            if n0 < pos < n1:
                return self._split_node(i, pos - n0)
        return True

    def _split_node(self, index: int, at: int) -> bool:
        """Split `self.nodes[index]` into two Content nodes at local offset `at`,
        the tail following the head in the document. False if the node is not held
        by an element that can take another child beside it."""
        node = self.nodes[index]
        text = node.text or ""
        if at <= 0 or at >= len(text):
            return True                         # already on a boundary
        parent = node.getparent()
        if parent is None:
            return False
        tail = ET.SubElement(parent, CONTENT)
        parent.remove(tail)
        tail.text = text[at:]
        node.text = text[:at]
        parent.insert(list(parent).index(node) + 1, tail)
        self.nodes.insert(index + 1, tail)
        return True

    def replace(self, start: int, end: int, new_text: str) -> None:
        """Replace the [start, end) span of this paragraph's text with
        `new_text`, editing the underlying Content nodes.

        The surviving prefix and suffix are kept on the first and last touched
        nodes; the replacement text lands on the first, and any node wholly
        inside the span is emptied. Formatting across the replaced span collapses
        to the first touched node's — the normal outcome of retyping a phrase,
        and never a concern for the punctuation and word swaps corrections are."""
        if start < 0 or end > len(self.text) or start > end:
            raise ValueError(f"span [{start}, {end}) outside paragraph "
                             f"of length {len(self.text)}")
        offset = 0
        first = last = None
        bounds = []
        for node in self.nodes:
            text = node.text or ""
            n0, n1 = offset, offset + len(text)
            bounds.append((node, n0, n1))
            if first is None and n0 <= start <= n1:
                first = (node, n0)
            if n0 <= end <= n1:
                last = (node, n0)          # keep updating; last wins ties at n1
            offset = n1
        if first is None or last is None:
            raise ValueError("could not locate the span in the paragraph")
        first_node, first_n0 = first
        last_node, last_n0 = last
        prefix = (first_node.text or "")[: start - first_n0]
        suffix = (last_node.text or "")[end - last_n0:]
        if first_node is last_node:
            first_node.text = prefix + new_text + suffix
            return
        first_node.text = prefix + new_text
        started = False
        for node, n0, n1 in bounds:
            if node is first_node:
                started = True
                continue
            if node is last_node:
                node.text = suffix
                break
            if started:
                node.text = ""


def _style_range(parent: ET._Element, nodes: list[ET._Element],
                 attrs: dict[str, str]) -> None:
    """Give `nodes` — a contiguous run of `parent`'s flow children — the character
    attributes in `attrs`, splitting `parent` around them if it holds anything else.

    An attribute whose value is None is removed instead of set, which is how
    "de-italicize" clears a local override rather than writing a second one over
    it."""
    flow = [c for c in parent if c.tag in FLOW]
    props = [c for c in parent if c.tag not in FLOW]
    first, last = flow.index(nodes[0]), flow.index(nodes[-1])
    if first == 0 and last == len(flow) - 1:
        _set_attrs(parent, attrs)               # the whole range is the span
        return
    grandparent = parent.getparent()
    at = list(grandparent).index(parent)
    pieces = [(flow[:first], False), (flow[first:last + 1], True),
              (flow[last + 1:], False)]
    made = []
    for children, styled in pieces:
        if not children:
            continue
        piece = ET.Element(CSR, dict(parent.attrib))
        for prop in props:
            piece.append(_copy(prop))
        for child in children:
            piece.append(child)                 # moves it out of `parent`
        if styled:
            _set_attrs(piece, attrs)
        made.append(piece)
    for offset, piece in enumerate(made):
        grandparent.insert(at + offset, piece)
    grandparent.remove(parent)


def _set_attrs(el: ET._Element, attrs: dict[str, str]) -> None:
    for name, value in attrs.items():
        if value is None:
            el.attrib.pop(name, None)
        else:
            el.set(name, value)


def _copy(el: ET._Element) -> ET._Element:
    clone = ET.Element(el.tag, dict(el.attrib))
    clone.text, clone.tail = el.text, el.tail
    for child in el:
        clone.append(_copy(child))
    return clone


@dataclass
class Story:
    """A story's paragraphs, over its parsed XML tree. Serialize back after
    editing."""
    story_id: str
    root: ET._Element
    paragraphs: list[Paragraph]

    @property
    def text(self) -> str:
        """The whole story as text, one newline between paragraphs — the shape
        the verifier diffs and word-counts."""
        return "\n".join(p.text for p in self.paragraphs)

    def serialize(self) -> bytes:
        return ET.tostring(self.root, xml_declaration=True, encoding="UTF-8",
                          standalone=True)


def parse_story(xml: bytes, story_id: str) -> Story:
    """Read a story part into paragraphs.

    Paragraphs break at every `<Br/>` and at every ParagraphStyleRange boundary
    — a paragraph style applies to whole paragraphs, so a paragraph never spans
    two ranges. Breaking on both is why an edit can never be told a span is
    within one paragraph when it is not."""
    root = ET.fromstring(xml)
    paragraphs: list[Paragraph] = []
    current = Paragraph(index=0, style="")

    def close():
        nonlocal current
        paragraphs.append(current)
        current = Paragraph(index=len(paragraphs), style="")

    for psr in root.iter(PSR):
        if current.nodes:                  # a range boundary is a paragraph break
            close()
        style = psr.get("AppliedParagraphStyle", "")
        current.style = style
        for el in _direct_content(psr):
            if el.tag == BR:
                close()
                current.style = style
            else:
                current.nodes.append(el)
    # The story's final paragraph carries no trailing Br; keep it if it has text.
    if current.nodes:
        paragraphs.append(current)
    # Renumber so indices are dense and in document order.
    for i, p in enumerate(paragraphs):
        p.index = i
    return Story(story_id=story_id, root=root, paragraphs=paragraphs)


def _direct_content(psr: ET._Element) -> list[ET._Element]:
    """The Content and Br elements that belong to this paragraph range itself,
    in document order — not those inside a nested table, footnote or range,
    which are reached when the walk gets to them on their own."""
    out: list[ET._Element] = []

    def walk(el: ET._Element) -> None:
        for child in el:
            if child.tag in NESTED:
                continue                   # a nested container; skip its subtree
            if child.tag in (CONTENT, BR):
                out.append(child)
            else:
                walk(child)

    walk(psr)
    return out


def content_story_names(names: list[str]) -> list[str]:
    """The user-text stories in an IDML package, backing story excluded."""
    return sorted(n for n in names if n.startswith(STORY_GLOB)
                  and n.endswith(".xml"))


def story_id_of(part_name: str) -> str:
    return part_name[len(STORY_GLOB):-len(".xml")]


def read_stories(idml_path: str | Path) -> list[Story]:
    """Every user-text story of an IDML, parsed for editing."""
    out: list[Story] = []
    with zipfile.ZipFile(Path(idml_path)) as z:
        for name in content_story_names(z.namelist()):
            out.append(parse_story(z.read(name), story_id_of(name)))
    return out


def rewrite_stories(src: str | Path, dest: str | Path,
                    changed: dict[str, bytes]) -> None:
    """Copy the IDML, swapping in the serialized bytes of the changed stories.

    mimetype stays first and stored; every other part is copied verbatim except
    the stories in `changed`, keyed by story id."""
    src, dest = Path(src), Path(dest)
    with zipfile.ZipFile(src) as zin:
        infos = zin.infolist()
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in infos:
                data = zin.read(info.filename)
                if info.filename.startswith(STORY_GLOB):
                    sid = story_id_of(info.filename)
                    if sid in changed:
                        data = changed[sid]
                if info.filename == "mimetype":
                    zout.writestr(info, data, compress_type=zipfile.ZIP_STORED)
                else:
                    zout.writestr(info, data)
