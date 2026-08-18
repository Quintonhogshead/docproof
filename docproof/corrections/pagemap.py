"""Which run of the book each page of the proof set.

A correction is marked on a page, and a page is the one thing an IDML does not
have: it is a flow of stories, paginated only when InDesign opens it. That gap is
why a mark on page 49 saying "replace this comma with a period" had the whole book
to land in — and a novel holds about seven thousand commas, so the edit could only
ever be flagged. The reviewer knew exactly which comma they meant; the file simply
had no way to hear it.

This closes the gap by aligning the text of each PDF page against the book's own
character stream, once, up front. The proof and the book are two renderings of the
same words, so the alignment is a search for where each page's words sit — done
against the normalized view (`textmatch`), because the PDF rendering carries
artifacts the book does not: hyphenation at line ends, kerning read back as
spaces, running heads and folios that belong to no story.

The result is a *narrower*, never a gate. A page it cannot place is reported as
unknown and the edit falls back to searching the whole book, exactly as before —
so a bad alignment can cost precision, never a correction.
"""
from __future__ import annotations

import logging
from array import array
from statistics import median

from .idml import Story
from .textmatch import NormIndex, normalize

log = logging.getLogger("docproof.corrections.pagemap")

# Length of each probe taken off a page, in normalized characters. Long enough
# that a probe is almost always unique in a book (~32 characters is five or six
# words), short enough that a probe is unlikely to straddle two artifacts and
# fail to match at all.
PROBE = 32
# How many probes to take across a page. Each one that lands votes on where the
# page sits; the rest are simply discarded, which is why an artifact-riddled page
# still aligns. A page that lands fewer than MIN_VOTES is left unplaced.
PROBES_PER_PAGE = 10
MIN_VOTES = 2
# How far apart two votes may be and still be believed to describe the same page.
# Pages disagree by the length of a running head or a folio, not by paragraphs.
VOTE_SPREAD = 400


class PageMap:
    """The book text each page of the proof covers, addressed the way `apply`
    addresses text: by story, paragraph and character span.

    `knows(page)` says whether the page was placed at all; `contains(...)` says
    whether a candidate match lies on it. Both are cheap — the work happens once,
    in `build_page_map`."""

    __slots__ = ("_ranges", "_pages")

    def __init__(self, ranges: dict[tuple[int, str, int], list[tuple[int, int]]],
                 pages: set[int]) -> None:
        self._ranges = ranges
        self._pages = pages

    def knows(self, page: int) -> bool:
        """Whether this page could be placed in the book at all."""
        return page in self._pages

    def contains(self, page: int, story_id: str, paragraph: int,
                 start: int, end: int) -> bool:
        """Whether the `[start, end)` span of that paragraph falls on that page."""
        for lo, hi in self._ranges.get((page, story_id, paragraph), ()):
            if lo <= start and end <= hi:
                return True
        return False

    def span_of(self, page: int) -> list[tuple[str, int, int, int]]:
        """Every (story_id, paragraph, start, end) run this page covers. For
        showing a reviewer, and for handing the extractor the book's own words for
        the page a comment sits on."""
        out = []
        for (pg, sid, para), spans in self._ranges.items():
            if pg == page:
                for lo, hi in spans:
                    out.append((sid, para, lo, hi))
        return sorted(out)

    @property
    def placed(self) -> int:
        return len(self._pages)


class _Stream:
    """The whole book as one normalized string, with every character traceable
    back to the paragraph and offset it came from.

    This is the coordinate space the page alignment works in. Two parallel arrays
    rather than a list of tuples: a novel is about a million characters, and the
    arrays keep that a few megabytes instead of tens."""

    def __init__(self, stories: list[Story]) -> None:
        self.slots: list[tuple[str, int]] = []      # slot -> (story_id, para)
        self.owner = array("i")                     # norm offset -> slot
        self.real = array("i")                      # norm offset -> real offset
        chunks: list[str] = []
        for story in stories:
            for para in story.paragraphs:
                slot = len(self.slots)
                self.slots.append((story.story_id, para.index))
                text = para.text
                view, src = _normalized_with_map(text)
                if not view:
                    continue
                chunks.append(view)
                self.owner.extend(array("i", [slot]) * len(view))
                self.real.extend(array("i", src))
        self.text = "".join(chunks)

    def ranges(self, start: int, end: int
               ) -> dict[tuple[str, int], list[tuple[int, int]]]:
        """The per-paragraph real spans covered by `[start, end)` of the stream.
        Consecutive stream characters from one paragraph collapse into one span,
        so a page becomes a handful of ranges rather than a million."""
        start = max(0, start)
        end = min(len(self.text), end)
        out: dict[tuple[str, int], list[tuple[int, int]]] = {}
        i = start
        while i < end:
            slot = self.owner[i]
            j = i
            while j < end and self.owner[j] == slot:
                j += 1
            key = self.slots[slot]
            lo, hi = self.real[i], self.real[j - 1] + 1
            out.setdefault(key, []).append((lo, hi))
            i = j
        return out


def page_book_text(stories: list[Story], page_map: PageMap, page: int) -> str:
    """The book's own words for one proof page, in reading order.

    This is the text an extractor should be quoting from. Until now the model was
    shown only the PDF's rendering of a page — hyphenated at the line ends, with
    kerning read back as spaces — and asked to produce a `find` that would match
    the IDML exactly. Handing it the IDML's text instead turns "recall a string
    that matches a document you have not seen" into "copy a phrase from this",
    which is a different and much easier task.

    Empty when the page was never placed."""
    if not page_map.knows(page):
        return ""
    by_para = {(s.story_id, p.index): p for s in stories for p in s.paragraphs}
    out: list[str] = []
    for story_id, para_index, lo, hi in page_map.span_of(page):
        para = by_para.get((story_id, para_index))
        if para is not None:
            run = para.text[lo:hi].strip()
            if run:
                out.append(run)
    return "\n".join(out)


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    """`textmatch.normalize` applied to one paragraph, plus the real offset each
    surviving character came from."""
    idx = NormIndex(text)
    return idx.view, idx.sources


def build_page_map(stories: list[Story], page_texts: list[str]) -> PageMap:
    """Align each page's text against the book and return the map.

    `page_texts[i]` is the text of proof page `i + 1`, as the PDF reader read it —
    artifacts and running heads included. Alignment is by vote: probes are taken
    across the page, each is looked for in the book, and each that lands offers an
    opinion about where the page begins. The median of the surviving votes places
    the page; a page whose probes cannot agree is left unplaced rather than put
    somewhere plausible."""
    if not page_texts:
        return PageMap({}, set())
    stream = _Stream(stories)
    if not stream.text:
        return PageMap({}, set())

    ranges: dict[tuple[int, str, int], list[tuple[int, int]]] = {}
    placed: set[int] = set()
    cursor = 0
    for i, raw in enumerate(page_texts):
        page = i + 1
        view = normalize(raw)
        if len(view) < PROBE:
            continue
        delta = _place(stream.text, view, cursor)
        if delta is None:
            continue
        placed.add(page)
        for (sid, para), spans in stream.ranges(delta, delta + len(view)).items():
            for span in spans:
                ranges.setdefault((page, sid, para), []).append(span)
        # Pages run in order, so the next one starts around where this one ended.
        # Backing off by a page's length keeps a mis-set cursor from stranding the
        # rest of the book, and `_place` falls back to a full scan anyway.
        cursor = max(0, delta + len(view) - len(view) // 2)
    log.info("Page map: placed %d of %d page(s) in the book",
             len(placed), len(page_texts))
    return PageMap(ranges, placed)


def _place(book: str, page: str, cursor: int) -> int | None:
    """Where `page` starts in `book`, or None if its probes cannot agree.

    Searched forward of `cursor` first, since pages run in order; on too few votes
    the whole book is scanned, because an IDML's stories are not necessarily in
    reading order and front matter can sit anywhere."""
    votes = _votes(book, page, cursor)
    if len(votes) < MIN_VOTES and cursor:
        votes = _votes(book, page, 0)
    if len(votes) < MIN_VOTES:
        return None
    # Keep the largest cluster of votes that agree, then take its median. A probe
    # that matched some other page's identical sentence is an outlier, and this is
    # what discards it instead of averaging it in.
    votes.sort()
    best: list[int] = []
    for i, v in enumerate(votes):
        group = [w for w in votes[i:] if w - v <= VOTE_SPREAD]
        if len(group) > len(best):
            best = group
    if len(best) < MIN_VOTES:
        return None
    return int(median(best))


def _votes(book: str, page: str, from_offset: int) -> list[int]:
    """One vote per probe that lands: the book offset the page would start at if
    that probe is right."""
    step = max(1, (len(page) - PROBE) // max(1, PROBES_PER_PAGE - 1))
    votes: list[int] = []
    for at in range(0, len(page) - PROBE + 1, step):
        probe = page[at:at + PROBE]
        hit = book.find(probe, from_offset)
        if hit != -1:
            votes.append(hit - at)
        if len(votes) >= PROBES_PER_PAGE:
            break
    return votes
