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
    # The concatenation, held until something changes it. Locating one correction
    # reads every paragraph's text once, so a book's worth of edits rebuilds this
    # from the lxml nodes millions of times over — and rebuilding it is a Python
    # loop over the nodes plus a fresh string. Only `replace` and `_split_node`
    # touch the nodes; every structural edit goes through `Story.reindex`, which
    # builds new Paragraph objects and so starts from an empty cache by
    # construction.
    _text: str | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def text(self) -> str:
        cached = self._text
        if cached is None:
            cached = self._text = "".join((n.text or "") for n in self.nodes)
        return cached

    def _text_changed(self) -> None:
        """Drop the cached concatenation. Called before any write to the nodes,
        so a stale read is not possible even if the write then bails out."""
        self._text = None

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

    def applied_character_styles(self, start: int, end: int) -> list[str]:
        """The `AppliedCharacterStyle` of every character range the `[start, end)`
        span touches, in order and without splitting anything.

        What a span is *already* set in decides how to change it: an IDML says
        "italic" either as a local `FontStyle` override or as an applied
        character style, and a request to take italics off has to clear the one
        that is actually there."""
        out, offset = [], 0
        for node in self.nodes:
            text = node.text or ""
            n0, n1 = offset, offset + len(text)
            offset = n1
            if not text or n1 <= start or n0 >= end:
                continue
            parent = node.getparent()
            if parent is not None and parent.tag == CSR:
                out.append(parent.get("AppliedCharacterStyle", ""))
        return out

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
        self._text_changed()
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
        inside the span is removed. Formatting across the replaced span collapses
        to the first touched node's — the normal outcome of retyping a phrase,
        and never a concern for the punctuation and word swaps corrections are.

        A removed interior node whose character range is left holding nothing else
        takes the range down with it, so removing the single quotes around an
        italicized aside does not strand an empty `<CharacterStyleRange>` where the
        aside used to be — the defect that put an italic run with no text into a
        finished file."""
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
        self._text_changed()
        prefix = (first_node.text or "")[: start - first_n0]
        suffix = (last_node.text or "")[end - last_n0:]
        if first_node is last_node:
            first_node.text = prefix + new_text + suffix
            return
        first_node.text = prefix + new_text
        started = False
        emptied: list[ET._Element] = []
        for node, n0, n1 in bounds:
            if node is first_node:
                started = True
                continue
            if node is last_node:
                node.text = suffix
                break
            if started:
                emptied.append(node)
        self._drop_nodes(emptied)

    def _drop_nodes(self, nodes: list[ET._Element]) -> None:
        """Remove each of `nodes` from the tree and from this paragraph's node
        list, and take down any `CharacterStyleRange` a removal leaves with no flow
        children — so an emptied interior run never lingers as empty XML."""
        if not nodes:
            return
        dropped = {id(n) for n in nodes}
        for node in nodes:
            parent = node.getparent()
            if parent is None:
                continue
            parent.remove(node)
            if parent.tag == CSR and not any(c.tag in FLOW for c in parent):
                grandparent = parent.getparent()
                if grandparent is not None:
                    grandparent.remove(parent)
        self.nodes = [n for n in self.nodes if id(n) not in dropped]


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


def _node_at(para: "Paragraph", offset: int) -> "ET._Element | None":
    """The last Content node of `para` that ends at `offset` — the node a split at
    that offset leaves behind. None when no node boundary falls there."""
    at = 0
    for node in para.nodes:
        at += len(node.text or "")
        if at == offset:
            return node
    return None


def _ancestor(el: ET._Element, tag: str) -> ET._Element | None:
    """The nearest enclosing element with that tag, or None."""
    node = el.getparent()
    while node is not None and node.tag != tag:
        node = node.getparent()
    return node


def _paragraph_groups(psr: ET._Element) -> list[list[tuple]]:
    """The range's flow children grouped into paragraphs, each group a list of
    `(owning character range, element)` ending at the `Br` that closes it."""
    groups: list[list[tuple]] = [[]]
    for csr in psr:
        if csr.tag != CSR:
            continue                       # a property of the range, not its text
        for child in csr:
            if child.tag not in FLOW:
                continue
            groups[-1].append((csr, child))
            if child.tag == BR:
                groups.append([])
    if groups and not groups[-1]:
        groups.pop()
    return groups


def _rebuild_ranges(psr: ET._Element, flat: list[tuple]) -> None:
    """Refill a fresh paragraph range with `(source range, element)` pairs, minting
    a character range per contiguous run that shared one. Each new range is cloned
    from the run's own source, so the applied style and font come with it."""
    current = source = None
    for src, el in flat:
        if src is not source:
            source = src
            current = ET.Element(CSR, dict(src.attrib))
            for prop in (c for c in src if c.tag not in FLOW):
                current.append(_copy(prop))
            psr.append(current)
        current.append(el)                 # moves it out of the old range


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

    def character_styles(self) -> set[str]:
        """Every `AppliedCharacterStyle` this story uses. What a book already has
        is what a correction should be written in, so "italicize this" can reach
        for the style the designer set the other italics in rather than laying a
        local override over the top of it."""
        return {csr.get("AppliedCharacterStyle", "")
                for csr in self.root.iter(CSR)}

    def serialize(self) -> bytes:
        return ET.tostring(self.root, xml_declaration=True, encoding="UTF-8",
                          standalone=True)

    def reindex(self) -> None:
        """Re-derive the paragraph list from the tree, after a structural change."""
        self.paragraphs = walk_paragraphs(self.root)

    def isolate(self, index: int) -> ET._Element | None:
        """Give the paragraph at `index` a `ParagraphStyleRange` of its own, and
        return it. None when the story's shape makes that impossible.

        Anything a reviewer asks *of a paragraph* — start this chapter on a
        right-hand page, make this a block quote, keep this heading with what
        follows — is a property of the range that encloses it. But a range routinely
        encloses several paragraphs (the test fixture's fourth holds four, separated
        by `Br`), so setting the property directly would apply it to all of them.
        This splits the range at the paragraph boundaries first, rebuilding each
        piece's character ranges from the originals so every applied style, font and
        property survives, and every break stays where it was.

        The paragraph list is unchanged by this — the same paragraphs in the same
        order — so an index taken before the call is still good after it."""
        if not 0 <= index < len(self.paragraphs):
            return None
        para = self.paragraphs[index]
        if not para.nodes:
            return None
        psr = _ancestor(para.nodes[0], PSR)
        if psr is None or any(c.tag in FLOW for c in psr):
            return None                    # flow text directly under the range
        groups = _paragraph_groups(psr)
        if len(groups) <= 1:
            return psr                     # already a range of its own
        target = next((i for i, g in enumerate(groups)
                       if any(el is para.nodes[0] for _, el in g)), None)
        if target is None:
            return None
        parent = psr.getparent()
        if parent is None:
            return None
        at = list(parent).index(psr)
        props = [c for c in psr if c.tag != CSR]
        made, mine = [], None
        for chunk in (groups[:target], groups[target:target + 1],
                      groups[target + 1:]):
            flat = [pair for g in chunk for pair in g]
            if not flat:
                continue
            piece = ET.Element(PSR, dict(psr.attrib))
            for prop in props:
                piece.append(_copy(prop))
            _rebuild_ranges(piece, flat)
            if chunk and chunk[0] is groups[target]:
                mine = piece
            made.append(piece)
        for offset, piece in enumerate(made):
            parent.insert(at + offset, piece)
        parent.remove(psr)
        self.reindex()
        return mine

    def set_paragraph_attrs(self, index: int, attrs: dict[str, str]) -> bool:
        """Set range attributes on one paragraph — `StartParagraph`,
        `AppliedParagraphStyle`, spacing. An attribute whose value is None is
        removed, which is how a forced break is cleared rather than overwritten."""
        psr = self.isolate(index)
        if psr is None:
            return False
        _set_attrs(psr, attrs)
        return True

    def delete_paragraph(self, index: int) -> bool:
        """Remove a paragraph entirely — its text and the break that ends it.

        Isolating first is what makes this safe: once the paragraph has a range to
        itself, deleting it is deleting that range, and no neighbour can be caught
        by the same cut. This is the operation the hand-written scripts got wrong in
        the other direction, merging two paragraphs into one."""
        psr = self.isolate(index)
        if psr is None:
            return False
        parent = psr.getparent()
        if parent is None:
            return False
        parent.remove(psr)
        self.reindex()
        return True

    def merge_paragraph(self, index: int, *, joiner: str = " ") -> bool:
        """Join the paragraph at `index` with the one after it, removing the break
        between them. False, changing nothing, when that cannot be done cleanly.

        This is `delete_paragraph`'s opposite, and the operation the hand-written
        scripts used to perform by accident. On a proof of verse it is asked for
        constantly: every line is its own paragraph, so "delete the line break
        between *stuff:* and *Rotting*" is one `Br` to remove — after which the two
        halves have to be joined by a space, because paragraph text carries no
        trailing one and the words would otherwise run together.

        Isolating both paragraphs first is what makes it safe: each then owns its
        range, so moving one range's contents into the other cannot catch a
        neighbour. Two paragraphs set in different styles are refused rather than
        merged — the surviving paragraph can only have one style, and silently
        imposing the first's on the second is the kind of change a reviewer asking
        about a line break did not ask for."""
        if not 0 <= index < len(self.paragraphs) - 1:
            return False
        if self.isolate(index) is None or self.isolate(index + 1) is None:
            return False
        first, second = self.paragraphs[index], self.paragraphs[index + 1]
        if not first.nodes or not second.nodes:
            return False
        psr_a = _ancestor(first.nodes[0], PSR)
        psr_b = _ancestor(second.nodes[0], PSR)
        if psr_a is None or psr_b is None or psr_a is psr_b:
            return False
        if psr_a.get("AppliedParagraphStyle") != psr_b.get("AppliedParagraphStyle"):
            return False
        parent = psr_b.getparent()
        if parent is None or psr_a.getparent() is not parent:
            return False
        # The break that closes the first paragraph is the thing being deleted.
        flow_a = [el for csr in psr_a if csr.tag == CSR
                  for el in csr if el.tag in FLOW]
        if not flow_a or flow_a[-1].tag != BR:
            return False
        br = flow_a[-1]
        br.getparent().remove(br)
        if joiner and not first.text.endswith(joiner) \
                and not second.text.startswith(joiner):
            last = flow_a[-2] if len(flow_a) > 1 else None
            if last is not None and last.tag == CONTENT:
                last.text = (last.text or "") + joiner
            else:
                return False
        for csr in [c for c in psr_b if c.tag == CSR]:
            psr_a.append(csr)                  # moves it out of psr_b
        parent.remove(psr_b)
        self.reindex()
        return True

    def split_paragraph(self, index: int, offset: int) -> bool:
        """Break the paragraph at `index` in two at character `offset`, so the text
        from there on becomes a paragraph of its own. False when the split point
        cannot be isolated.

        The whitespace immediately before the split goes with the break: a
        paragraph break already separates the two halves, and leaving the space
        that used to would put a stray one at the end of the first paragraph."""
        if not 0 <= index < len(self.paragraphs):
            return False
        para = self.paragraphs[index]
        if not 0 < offset <= len(para.text):
            return False
        if self.isolate(index) is None:
            return False
        para = self.paragraphs[index]
        text = para.text
        head = offset
        while head > 0 and text[head - 1] in " \t":
            head -= 1
        if head == 0:
            return False                       # nothing would be left in front
        if head < offset:
            para.replace(head, offset, "")
        if not para._split_at(head):
            return False
        # The break closes the first half, so it goes after the last node in front
        # of the split — inside the character range that holds it, which is where
        # a Br lives.
        cut = _node_at(para, head)
        if cut is None:
            return False
        csr = _ancestor(cut, CSR)
        if csr is None:
            return False
        csr.insert(list(csr).index(cut) + 1, ET.Element(BR))
        self.reindex()
        return True

    def insert_paragraph(self, index: int, text: str, *, style: str = "",
                         after: bool = True) -> bool:
        """Add a paragraph beside the one at `index`, taking its style unless one is
        named. This is how deleted copy is put back — an Acknowledgments page that a
        previous pass dropped is paragraphs and a page break, not layout."""
        psr = self.isolate(index)
        if psr is None:
            return False
        parent = psr.getparent()
        if parent is None:
            return False
        template = next((c for c in psr if c.tag == CSR), None)
        piece = ET.Element(PSR, {"AppliedParagraphStyle":
                                 style or psr.get("AppliedParagraphStyle", "")})
        csr = ET.Element(CSR, dict(template.attrib) if template is not None else {})
        if template is not None:
            for prop in (c for c in template if c.tag not in FLOW):
                csr.append(_copy(prop))
        content = ET.SubElement(csr, CONTENT)
        content.text = text
        ET.SubElement(csr, BR)
        piece.append(csr)
        # A story's last paragraph carries no closing break. Inserting after it means
        # it needs one, or the two run together into a single paragraph.
        if after and template is not None:
            flow = [c for c in psr.iter() if c.tag in (CONTENT, BR)]
            if flow and flow[-1].tag != BR:
                last_csr = _ancestor(flow[-1], CSR)
                if last_csr is not None:
                    last_csr.append(ET.Element(BR))
        parent.insert(list(parent).index(psr) + (1 if after else 0), piece)
        self.reindex()
        return True


def parse_story(xml: bytes, story_id: str) -> Story:
    """Read a story part into paragraphs.

    Paragraphs break at every `<Br/>` and at every ParagraphStyleRange boundary
    — a paragraph style applies to whole paragraphs, so a paragraph never spans
    two ranges. Breaking on both is why an edit can never be told a span is
    within one paragraph when it is not."""
    root = ET.fromstring(xml)
    return Story(story_id=story_id, root=root,
                 paragraphs=walk_paragraphs(root))


def walk_paragraphs(root: ET._Element) -> list[Paragraph]:
    """The paragraphs of a parsed story tree, in document order.

    Split out of `parse_story` so a structural edit — a paragraph inserted, one
    removed, a range split so a single paragraph can be styled — can re-derive the
    list from the tree it just changed, instead of the caller holding indices that
    no longer mean anything."""
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
    for i, p in enumerate(paragraphs):
        p.index = i
    return paragraphs


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
