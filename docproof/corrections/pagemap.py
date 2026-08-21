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
import re
from array import array
from statistics import median

from .idml import Paragraph, Story
from .textmatch import NormIndex, normalize

log = logging.getLogger("docproof.corrections.pagemap")

# Length of each probe taken off a page, in normalized characters. Long enough
# that a probe is almost always unique in a book (~32 characters is five or six
# words), short enough that a probe is unlikely to straddle two artifacts and
# fail to match at all.
PROBE = 32
# The shortest probe still distinctive enough to trust on its own. A page too
# short to yield a full PROBE — a title page ("DIAGNOSIS"), a section divider, a
# one-line poem — cannot gather votes, but a run this long that occurs exactly
# once in the book is an unambiguous anchor (see `_place_unique`): uniqueness, not
# length, is what makes the placement safe, so this can be about a word long. A
# run shorter still is too likely to occur once by coincidence — as a substring of
# a longer word — so the page is left unplaced rather than risked.
MIN_PROBE = 8
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

    __slots__ = ("_ranges", "_pages", "_proof")

    def __init__(self, ranges: dict[tuple[int, str, int], list[tuple[int, int]]],
                 pages: set[int], proof: tuple[str, ...] = ()) -> None:
        self._ranges = ranges
        self._pages = pages
        self._proof = proof

    def knows(self, page: int) -> bool:
        """Whether this page could be placed in the book at all."""
        return page in self._pages

    def proof_text(self, page: int) -> str:
        """The proof's own rendered text for `page` (1-based), or "" out of range.

        This is what the *proof* printed on the page, independent of where the map
        managed to align it in the book — the direct record a caller consults before
        deciding an edit is on the wrong page. The alignment can under-cover a page;
        the proof cannot."""
        if 1 <= page <= len(self._proof):
            return self._proof[page - 1]
        return ""

    def contains(self, page: int, story_id: str, paragraph: int,
                 start: int, end: int) -> bool:
        """Whether the `[start, end)` span of that paragraph falls on that page."""
        for lo, hi in self._ranges.get((page, story_id, paragraph), ()):
            if lo <= start and end <= hi:
                return True
        return False

    def page_of(self, story_id: str, paragraph: int) -> int:
        """The lowest proof page whose run covers any of that paragraph, or 0
        when no placed page does. The reverse of `contains`, for labelling a
        candidate with the page a designer would look for it on."""
        pages = [pg for (pg, sid, para) in self._ranges
                 if sid == story_id and para == paragraph]
        return min(pages) if pages else 0

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


def page_book_text(stories: list[Story], page_map: PageMap, page: int, *,
                   by_para: dict | None = None) -> str:
    """The book's own words for one proof page, in reading order.

    This is the text an extractor should be quoting from. Until now the model was
    shown only the PDF's rendering of a page — hyphenated at the line ends, with
    kerning read back as spaces — and asked to produce a `find` that would match
    the IDML exactly. Handing it the IDML's text instead turns "recall a string
    that matches a document you have not seen" into "copy a phrase from this",
    which is a different and much easier task.

    `by_para` is the paragraph lookup, for a caller asking about many pages: it
    covers the whole book, so building it per page is a walk of the novel per
    proof page. `paragraph_lookup` builds it; passing nothing builds a private
    one, which is what a single-page caller wants.

    Empty when the page was never placed."""
    if not page_map.knows(page):
        return ""
    if by_para is None:
        by_para = paragraph_lookup(stories)
    out: list[str] = []
    for story_id, para_index, lo, hi in page_map.span_of(page):
        para = by_para.get((story_id, para_index))
        if para is not None:
            run = para.text[lo:hi].strip()
            if run:
                out.append(run)
    return "\n".join(out)


def paragraph_lookup(stories: list[Story]) -> dict[tuple[str, int], Paragraph]:
    """Every paragraph of the book, keyed by (story id, index) — the lookup
    `page_book_text` reads a page's spans out of."""
    return {(s.story_id, p.index): p for s in stories for p in s.paragraphs}


# --- the folios the proof itself prints ----------------------------------------
# The fallback for a proof whose page count does not match the file's (a cover
# the file does not carry, a spread exported as one page): the folio printed at
# the head or foot of each page is direct evidence of what InDesign calls it,
# and it rides in the page's own extracted text.

# A folio as it appears in a page's text: a line that is nothing but a 1-4 digit
# number, or a number that opens or closes one of the page's edge lines (a
# running head reads "42  THE LONG ROAD" or "AUTHOR NAME  42").
_FOLIO_LINE = re.compile(r"^\d{1,4}$")
_FOLIO_EDGE = re.compile(r"^(\d{1,4})\b|\b(\d{1,4})$")
# How many of a page's first and last non-blank lines may hold the folio.
_EDGE_LINES = 2
# The least evidence worth trusting: this many pages must print a folio, and at
# least this many (and half of all read) must agree on one page-to-folio offset,
# before any label is written. A novel's body prints one on nearly every page,
# so a real proof clears this easily and a coincidence of stray numbers never
# does.
_MIN_READS = 8


def _printed_number(text: str) -> int | None:
    """The folio this page prints, or None. Only the page's edges are read — a
    number in running text is body copy, not a folio."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    edges = lines[:_EDGE_LINES] + lines[-_EDGE_LINES:]
    for ln in edges:
        if _FOLIO_LINE.match(ln):
            return int(ln)
    for ln in edges:
        m = _FOLIO_EDGE.search(ln)
        if m:
            return int(m.group(1) or m.group(2))
    return None


def printed_folio_labels(page_texts) -> dict[int, str]:
    """Which folio each 1-based proof page prints, read off the proof's own text
    and accepted only as one consistent run.

    The validation is the point: any one number on a page could be anything, but
    a book's body is a single arithmetic sequence — folio minus physical page is
    one constant offset from the first body page to the last. So every page's
    candidate votes on that offset, the winning offset must carry at least half
    the votes, and only then is the run labelled — every page inside it, folio =
    page + offset, including the chapter openers and section breaks that print
    no folio of their own. Front matter numbered apart (or not at all) falls
    outside the run and keeps its physical page: a lost label, never a wrong
    one."""
    reads: dict[int, int] = {}
    for i, text in enumerate(page_texts or (), 1):
        n = _printed_number(text)
        if n is not None:
            reads[i] = n
    if len(reads) < _MIN_READS:
        return {}
    votes: dict[int, int] = {}
    for page, folio in reads.items():
        votes[folio - page] = votes.get(folio - page, 0) + 1
    offset, count = max(votes.items(), key=lambda kv: kv[1])
    if count < max(_MIN_READS, (len(reads) + 1) // 2):
        return {}
    matching = [p for p, f in reads.items() if f - p == offset]
    lo, hi = min(matching), max(matching)
    return {p: str(p + offset) for p in range(lo, hi + 1)}


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
    proof = tuple(page_texts)
    stream = _Stream(stories)
    if not stream.text:
        return PageMap({}, set(), proof)

    ranges: dict[tuple[int, str, int], list[tuple[int, int]]] = {}
    placed: set[int] = set()
    cursor = 0
    for i, raw in enumerate(page_texts):
        page = i + 1
        view = normalize(raw)
        if len(view) < MIN_PROBE:
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
    return PageMap(ranges, placed, proof)


def _place(book: str, page: str, cursor: int) -> int | None:
    """Where `page` starts in `book`, or None if it cannot be placed.

    Searched forward of `cursor` first, since pages run in order; on too few votes
    the whole book is scanned, because an IDML's stories are not necessarily in
    reading order and front matter can sit anywhere.

    Two ways to place a page, tried in order of how much text it has. A page with
    enough text votes: probes are scattered across it and the largest cluster that
    agrees carries it — robust to a running head or a stray artifact matching
    elsewhere. A page too short to gather that many probes — a title page, a
    section divider, a one-line poem — cannot vote, so it falls back to a single
    probe that occurs *exactly once* in the whole book, which pins it beyond doubt
    on its own. Sparse pages are exactly the ones a mark most needs placed, and are
    exactly the ones the vote path silently dropped before."""
    probe_len = min(PROBE, len(page))
    votes = _votes(book, page, cursor, probe_len)
    if len(votes) < MIN_VOTES and cursor:
        votes = _votes(book, page, 0, probe_len)
    if len(votes) >= MIN_VOTES:
        # Keep the largest cluster of votes that agree, then take its median. A
        # probe that matched some other page's identical sentence is an outlier,
        # and this is what discards it instead of averaging it in.
        votes.sort()
        best: list[int] = []
        for i, v in enumerate(votes):
            group = [w for w in votes[i:] if w - v <= VOTE_SPREAD]
            if len(group) > len(best):
                best = group
        if len(best) >= MIN_VOTES:
            return int(median(best))
    # Too few probes agreed (or the page was too short to take PROBE-length ones).
    # A run that appears exactly once in the book still places the page outright.
    return _place_unique(book, page, probe_len)


def _votes(book: str, page: str, from_offset: int, probe_len: int) -> list[int]:
    """One vote per probe that lands: the book offset the page would start at if
    that probe is right."""
    step = max(1, (len(page) - probe_len) // max(1, PROBES_PER_PAGE - 1))
    votes: list[int] = []
    for at in range(0, len(page) - probe_len + 1, step):
        probe = page[at:at + probe_len]
        hit = book.find(probe, from_offset)
        if hit != -1:
            votes.append(hit - at)
        if len(votes) >= PROBES_PER_PAGE:
            break
    return votes


def _place_unique(book: str, page: str, probe_len: int) -> int | None:
    """Place a page on a single probe that occurs exactly once in the book.

    A page that could not vote is one there was too little of to scatter probes
    across — but too little text can still be *distinctive* text. A run that the
    whole book carries exactly once has only one place the page can sit, so a lone
    match on it is not a guess. The whole page is the most distinctive probe, so it
    is tried first; then standard-length windows across it, so a page whose text is
    a distinctive phrase wrapped in an artifact still finds its one anchor. A run
    that repeats tells us nothing and is passed over — the page stays unplaced
    rather than placed on a coin toss."""
    n = len(page)
    windows: list[tuple[int, int]] = []
    if n <= PROBE and n >= MIN_PROBE:
        windows.append((0, n))                 # the whole short page, whole
    length = min(PROBE, n)
    if length >= MIN_PROBE:
        step = max(1, (n - length) // max(1, PROBES_PER_PAGE - 1))
        windows += [(at, length) for at in range(0, n - length + 1, step)]
    for at, length in windows:
        probe = page[at:at + length]
        first = book.find(probe)
        if first != -1 and book.find(probe, first + 1) == -1:
            return first - at
    return None
