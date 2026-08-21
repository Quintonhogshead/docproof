"""Reading a commented PDF proof into corrections.

An author or editor marks up a PDF of the book — sticky-note comments and
highlights, each carrying a correction. The comment text lives in the PDF's
annotation layer, not the rendered page; this pulls each one out together with
the manuscript text it points at — a highlight's own span (from its QuadPoints),
or the line a point-note sits on (matched by position) — so the model extractor
can turn the pair into one exact find/replace edit.

Two libraries, each for the thing it is good at. pypdf reads the annotation
layer: the comment text, the subtype, the QuadPoints. pdfplumber reads the page
geometry: every word's exact bounding box, in the same PDF user space the
annotations are in, so resolving a highlight to the words under it is an
intersection of rectangles and nothing more.

That split is deliberate, and it replaces a measurement. The reader used to walk
each glyph out from its run's start by its width from the font's `/Widths` table,
scaled by a single points-per-em estimated for the whole page — and on justified
body text, where the word spaces are stretched to fill the measure, that estimate
drifts. Far enough in, a highlight's words are no longer where the arithmetic says
they are: the mark reads back as the wrong words, or as nothing at all. Exact boxes
remove the estimate, and with it that whole class of silently wrong anchor.

The reading here is deterministic; the interpretation is the model's, downstream
in `extract`, and a person reviews the draft before anything is applied. This is
why a cryptic note ("iPhone", "make this a question") is still useful: it arrives
paired with the exact line it annotates, which is the context the model (and the
reviewer) needs.

No OCR. A scanned proof (an image with no text or annotation layer) yields nothing
here and is reported as such rather than guessed at.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger("docproof.corrections.from_pdf")

# Annotation subtypes that carry a correction. A Popup is only the on-screen box
# for one of these and holds no content of its own, so it is ignored.
_COMMENT_SUBTYPES = {"/Text", "/Highlight", "/FreeText"}
# How near (in points) a point-note's anchor has to be to a line of text to be
# read as sitting on it. Lines are ~15pt apart; a note dropped between two lines
# still lands on the nearer one.
_LINE_TOLERANCE = 10.0
# How much of a word has to fall inside a highlight box to count as marked. A
# highlight drawn by hand overshoots and undershoots by a fraction of a character
# at each end, so requiring the whole word would drop the first and last word of
# most spans, and requiring a single point would pull in the neighbours.
_WORD_OVERLAP = 0.35


@dataclass(frozen=True)
class PdfComment:
    """One correction read off the PDF: what the reviewer wrote, and the
    manuscript text it is anchored to."""
    page: int                 # 1-based
    instruction: str          # the comment text
    anchor: str               # the highlighted span, or the words nearest a note
    context: str              # the whole line(s) around the anchor
    kind: str                 # "highlight" | "note"
    # A stable id, unique across the read, carried to the model and echoed back on
    # every edit it produces. It is what lets the finished change log account for
    # this comment — applied, flagged, or turned into nothing — rather than have it
    # vanish when no edit is made. `p{page}-{n}` reads for a human skimming a log.
    id: str = ""
    # Where `anchor` starts in the page's own text. The page map turns a page into
    # a run of book text, so this is how far into that run the mark sat — the
    # closest thing a page-anchored correction has to an exact address.
    offset: int = -1
    # What the author wrote back on this mark, in order. A proof often makes a
    # second pass through the author, who answers the proofreader's queries in
    # place — a reply annotation saying "correct", "stet", or "please change to
    # 'leaves'". The reply is the author's decision on the mark it hangs off, so
    # it rides with the mark instead of arriving as a mark of its own — an
    # unanchored "correct" floating beside its question was a comment the model
    # could only guess at.
    replies: tuple[str, ...] = ()


@dataclass(frozen=True)
class PdfProof:
    """Everything one marked-up proof yields: the comments, and the text of every
    page. The page texts are what `pagemap` aligns against the book, which is what
    lets a mark on page 49 narrow to the text page 49 actually set."""
    comments: tuple[PdfComment, ...] = ()
    page_texts: tuple[str, ...] = ()


@dataclass
class _Word:
    """One word of a page, with its box in PDF user space and its offset in the
    page's text."""
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    offset: int
    line: int


@dataclass
class _Line:
    """One line of a page: its vertical band, and the span of the page text it
    occupies."""
    y0: float
    y1: float
    start: int
    end: int
    words: list[_Word] = field(default_factory=list)

    @property
    def centre(self) -> float:
        return (self.y0 + self.y1) / 2


class _Page:
    """A page's words and lines, read off pdfplumber's exact boxes.

    Lines are joined with a newline and words within a line with a single space,
    so the page text reads as the page reads — and every word knows where it sits
    in that string, which is what turns a rectangle on the page into a span of
    text."""

    def __init__(self, plumber_page) -> None:
        height = float(plumber_page.height)
        raw = []
        for w in plumber_page.extract_words():
            raw.append((float(w["x0"]), float(w["x1"]),
                        height - float(w["bottom"]), height - float(w["top"]),
                        str(w["text"])))
        # Reading order: top line first, left to right within a line. The y of a
        # line is not exact to the point (a superscript, a different face), so
        # words are gathered into bands rather than compared for equality.
        raw.sort(key=lambda r: (-(r[2] + r[3]) / 2, r[0]))
        self.words: list[_Word] = []
        self.lines: list[_Line] = []
        parts: list[str] = []
        cursor = 0
        for x0, x1, y0, y1, text in raw:
            centre = (y0 + y1) / 2
            line = self.lines[-1] if self.lines else None
            if line is None or abs(line.centre - centre) > _LINE_TOLERANCE:
                if line is not None:
                    parts.append("\n")
                    cursor += 1
                    line.end = cursor - 1
                line = _Line(y0=y0, y1=y1, start=cursor, end=cursor)
                self.lines.append(line)
            elif line.words:
                parts.append(" ")
                cursor += 1
            word = _Word(text=text, x0=x0, x1=x1, y0=y0, y1=y1, offset=cursor,
                         line=len(self.lines) - 1)
            parts.append(text)
            cursor += len(text)
            line.words.append(word)
            line.y0 = min(line.y0, y0)
            line.y1 = max(line.y1, y1)
            self.words.append(word)
        if self.lines:
            self.lines[-1].end = cursor
        self.text = "".join(parts)

    def line_text(self, index: int) -> str:
        line = self.lines[index]
        return self.text[line.start:line.end]

    def in_boxes(self, boxes: list[tuple[float, float, float, float]]
                 ) -> tuple[str, int, list[int]]:
        """The text a highlight's rectangles cover, its offset in the page text,
        and the lines it touched.

        A word counts as marked when enough of its width falls inside a box. The
        span returned runs unbroken from the first marked word to the last, so a
        short interior word a hand-drawn highlight skipped is filled in rather
        than dropped — `"some of the"`, never `"some the"`."""
        marked = [i for i, w in enumerate(self.words)
                  if _overlaps(w, boxes)]
        if not marked:
            return "", -1, []
        lo, hi = min(marked), max(marked)
        start = self.words[lo].offset
        end = self.words[hi].offset + len(self.words[hi].text)
        lines = sorted({self.words[i].line for i in range(lo, hi + 1)})
        return self.text[start:end], start, lines

    def line_at(self, y0: float, y1: float | None = None) -> int:
        """The line a point-note belongs to, from the vertical band of its box.

        A line the box actually *touches* wins, by how much of it the box covers.
        Only when the box lands wholly in the leading between two lines does the
        "nearest one at or above" rule apply — a note's icon hangs just under the
        word it marks, so a mark in the gap belongs to the line above it.

        Touching has to be tested before hanging-under, and that is the whole
        repair here. The old rule asked only whether a line's *bottom* edge was
        above the anchor point, so a note whose box was short — many PDF tools
        store a zero-height `/Rect` for a sticky note, because the viewer draws a
        fixed-size icon and ignores the stored one — put the anchor point *inside*
        the band of the line it marked. That line then failed the test against its
        own bottom edge, and the line above it was returned: every such note
        reached the extractor quoting the line before the one it was written
        about."""
        if not self.lines:
            return -1
        if y1 is None:
            y1 = y0
        low, high = min(y0, y1), max(y0, y1)
        # Overlap of the box with each line's band. Zero counts: a box of no height
        # sitting inside a band overlaps it by nothing and belongs to it entirely.
        overlaps = [min(ln.y1, high) - max(ln.y0, low) for ln in self.lines]
        best = max(range(len(self.lines)), key=lambda i: overlaps[i])
        if overlaps[best] >= 0:
            return best
        above = [i for i, ln in enumerate(self.lines) if ln.y0 >= low - 2.0]
        if above:
            return min(above, key=lambda i: self.lines[i].y0 - low)
        return min(range(len(self.lines)),
                   key=lambda i: abs(self.lines[i].centre - low))


# How far outside a highlight's band a word's centre may sit and still be read as
# inside it. Small on purpose: it forgives a box drawn a shade short of the glyphs,
# and is far less than the ~16pt between one line's centre and the next, so it can
# never reach a neighbouring line.
_BOX_SLACK = 2.0


def _overlaps(word: _Word, boxes) -> bool:
    """Whether enough of `word`'s width lies inside any of the highlight boxes.

    The line test is on the word's *centre*, not on whether its band comes within
    a couple of points of the box. A highlight drawn over one word reaches into
    the leading above and below it, and the leading is only a few points wide, so
    a tolerance big enough to forgive a thin box was also big enough to reach the
    line above — where it caught the words sitting over the marked one. The
    anchor then ran from there to the marked word, and the correction arrived
    quoting the line before the one it was written about. A centre cannot be in
    two lines at once, which is the property this needs."""
    width = max(word.x1 - word.x0, 0.01)
    centre = (word.y0 + word.y1) / 2
    for x0, x1, y0, y1 in boxes:
        if not y0 - _BOX_SLACK <= centre <= y1 + _BOX_SLACK:
            continue                              # a different line
        covered = min(word.x1, x1) - max(word.x0, x0)
        if covered / width >= _WORD_OVERLAP:
            return True
    return False


def read_pdf(path: str | Path) -> PdfProof:
    """Read a marked-up proof: every comment paired with the text it points at,
    and the text of every page.

    A PDF with no comment annotations yields no comments; the page texts are still
    returned, since they are what a page map needs and a proof can be read for its
    pages alone."""
    import pdfplumber
    import pypdf

    reader = pypdf.PdfReader(str(path))
    comments: list[PdfComment] = []
    # Which comment each annotation object became, by object number — how a
    # reply annotation (`/IRT`, "in reply to") finds the mark it answers.
    by_ref: dict[int, int] = {}
    page_texts: list[str] = []
    with pdfplumber.open(str(path)) as plumber:
        for pi, plumber_page in enumerate(plumber.pages):
            page_no = pi + 1
            try:
                layout = _Page(plumber_page)
            except Exception:              # noqa: BLE001 - a page we can't read
                log.warning("Could not read the text of page %d", page_no,
                            exc_info=True)
                layout = None
            finally:
                # pdfplumber caches every object it parsed on the page, and holds it
                # for as long as the document is open. Over a 400-page proof that
                # accumulates into hundreds of megabytes on a box with two gigabytes
                # to share with a JVM; the words are already copied out by here, so
                # the cache has nothing left to give.
                plumber_page.close()
            page_texts.append(layout.text if layout is not None else "")
            if pi >= len(reader.pages):
                continue
            annots = reader.pages[pi].get("/Annots")
            if not annots or layout is None:
                continue
            # Replies after the marks they answer: a reply's parent is on the
            # same page (the thread renders there), so by the second pass it has
            # a comment to be folded into.
            replies = []
            for ref in annots:
                if ref.get_object().get("/IRT") is not None:
                    replies.append(ref)
                    continue
                comment = _read_annot(ref, layout, page_no, len(comments))
                if comment is not None:
                    by_ref[_obj_id(ref)] = len(comments)
                    comments.append(comment)
            for ref in replies:
                _fold_reply(ref, comments, by_ref, layout, page_no)
    log.info("Read %d comment(s) over %d page(s) from %s",
             len(comments), len(page_texts), Path(path).name)
    return PdfProof(comments=tuple(comments), page_texts=tuple(page_texts))


def _read_annot(ref, layout: _Page, page_no: int, seen: int) -> PdfComment | None:
    """One annotation as a `PdfComment`, or None when it carries no correction."""
    obj = ref.get_object()
    if str(obj.get("/Subtype")) not in _COMMENT_SUBTYPES:
        return None
    contents = obj.get("/Contents")
    if not contents:
        return None                       # a bare Popup / drawing, no comment
    instruction = str(contents).strip()
    if not instruction:
        return None

    quad = obj.get("/QuadPoints")
    if quad:
        boxes = _quad_boxes([float(v) for v in quad])
        anchor, offset, lines = layout.in_boxes(boxes)
        context = " ".join(layout.line_text(i) for i in lines) if lines else anchor
        kind = "highlight"
        if not anchor:
            # The mark resolved to no word — an empty region, or a highlight over
            # an image. Fall back to the line its box sits on so the comment still
            # arrives anchored to something real.
            i = layout.line_at(min(b[2] for b in boxes),
                               max(b[3] for b in boxes))
            if i >= 0:
                anchor = context = layout.line_text(i)
                offset = layout.lines[i].start
    else:
        rect = obj.get("/Rect")
        kind = "note"
        anchor = context = ""
        offset = -1
        if rect:
            i = layout.line_at(float(rect[1]), float(rect[3]))
            if i >= 0:
                anchor = context = layout.line_text(i)
                offset = layout.lines[i].start

    # A per-page running number keeps the id stable and legible.
    n = 1 + seen
    return PdfComment(page=page_no, instruction=instruction,
                      anchor=_norm(anchor), context=_norm(context), kind=kind,
                      id=f"p{page_no}-{n}", offset=offset)


def _obj_id(ref) -> int:
    """The PDF object number behind an annotation reference — the identity a
    reply's `/IRT` points back at. -1 for an object pypdf handed over already
    resolved with no reference to give."""
    idnum = getattr(ref, "idnum", None)
    if idnum is None:
        idnum = getattr(getattr(ref, "indirect_reference", None), "idnum", None)
    return -1 if idnum is None else int(idnum)


def _fold_reply(ref, comments: list[PdfComment], by_ref: dict[int, int],
                layout: _Page, page_no: int) -> None:
    """Attach one reply annotation to the comment it answers.

    The chain is followed to its root — an author replying to a proofreader's
    reply is still answering the original mark. A reply whose root never became a
    comment (a bare drawing, a subtype this reader skips) falls back to being
    read as a mark of its own, anchored to the line it sits on: worse than a
    thread, better than losing what the author said."""
    obj = ref.get_object()
    text = str(obj.get("/Contents") or "").strip()
    if not text:
        return
    parent, root = obj.get("/IRT"), None
    for _ in range(8):                     # bounded — a cycle proves nothing
        if parent is None:
            break
        root = parent
        parent = parent.get_object().get("/IRT")
    at = by_ref.get(_obj_id(root)) if root is not None else None
    if at is None:
        comment = _read_annot(ref, layout, page_no, len(comments))
        if comment is not None:
            by_ref[_obj_id(ref)] = len(comments)
            comments.append(comment)
        return
    comments[at] = replace(comments[at],
                           replies=comments[at].replies + (text,))


def _quad_boxes(quad: list[float]) -> list[tuple[float, float, float, float]]:
    """QuadPoints as `(x0, x1, y0, y1)` boxes, one per highlighted rectangle,
    merged where they share a line.

    QuadPoints come in eights: x1 y1 … x4 y4 per rectangle. A word-by-word
    highlight is one rectangle per word, so they are folded per line before the
    words are read — otherwise each rectangle re-reads the words its neighbour
    already covered."""
    boxes: list[list[float]] = []
    for i in range(0, len(quad) - 7, 8):
        xs = quad[i:i + 8:2]
        ys = quad[i + 1:i + 8:2]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        centre = (y0 + y1) / 2
        for b in boxes:
            if abs((b[2] + b[3]) / 2 - centre) <= _LINE_TOLERANCE:
                b[0], b[1] = min(b[0], x0), max(b[1], x1)
                b[2], b[3] = min(b[2], y0), max(b[3], y1)
                break
        else:
            boxes.append([x0, x1, y0, y1])
    return [(b[0], b[1], b[2], b[3]) for b in boxes]


def read_pdf_comments(path: str | Path) -> list[PdfComment]:
    """Every comment in the PDF, paired with the text it points at. Empty if the
    PDF carries no comment annotations (e.g. a plain or scanned proof)."""
    return list(read_pdf(path).comments)


def _norm(s: str) -> str:
    return " ".join(s.split())


def comments_source(comments: list[PdfComment],
                    pages: dict[int, str] | None = None) -> str:
    """Format the comments as the corrections source the model extractor reads:
    each comment beside the exact manuscript text it annotates, so the model can
    quote a real anchor rather than guess one.

    `pages` maps a page number to **the book's own text for that page**, read out of
    the IDML the edits will be applied to. When it is given, the source leads with
    it and the model is told to copy its anchors from there. That is the difference
    between two tasks: recalling a string that will match a document the model has
    never seen, and copying a phrase out of one in front of it. Everything the PDF
    rendering does to a line — hyphenating it at the measure, reading a kern back as
    a space — stops mattering, because the line is no longer what is being quoted."""
    pages = pages or {}
    lines = [
        "Corrections marked on a PDF proof of a book. Each item is a reviewer's "
        "comment and the manuscript text it points at — a highlighted span, or "
        "the line a sticky note sits on. Turn each into one exact find/replace "
        "edit. Each item carries an id (e.g. p3-2); set the edit's `source` to "
        "that id so every comment can be accounted for. One edit per item — never "
        "drop one.",
    ]
    if pages:
        lines += [
            "",
            "THE BOOK'S OWN TEXT follows, page by page. Copy every `find` and "
            "`context` from it, character for character — it is the text the edit "
            "is applied to, and an anchor that is not in it cannot be located. The "
            "highlighted text quoted with each comment below comes from the PDF "
            "instead, so it may be hyphenated at a line end, carry a space the "
            "typesetting put in, or stop mid-word: use it to see WHICH words a "
            "comment is about, then quote those words from the book text here.",
            "",
        ]
        for page in sorted(pages):
            if pages[page].strip():
                lines.append(f"[book text, page {page}]")
                lines.append(pages[page])
                lines.append("")
        lines.append("--- the reviewer's comments ---")
    if any(c.replies for c in comments):
        lines += [
            "",
            "Some comments carry the author's reply — the author's decision on "
            "the reviewer's note, and it is final. A confirmation (\"correct\", "
            "\"yes\", \"please change\") means make the change the note "
            "describes, as an exact edit, even where the note alone was only a "
            "question. A reply that gives its own wording or instruction "
            "OVERRULES the note: carry out the author's correction and do NOT "
            "apply the reviewer's — the note is only there to show which text the "
            "author is correcting. A rejection (\"no\", \"stet\", \"leave as "
            "is\", \"keep\") means change nothing: emit the edit with `replace` "
            "equal to `find`, so the mark is recorded as resolved without a "
            "change.",
        ]
    lines.append("")
    for i, c in enumerate(comments, 1):
        cid = c.id or f"c{i}"
        lines.append(f"{i}. (id {cid}) [page {c.page}] comment: \"{c.instruction}\"")
        for reply in c.replies:
            lines.append(f"   the author's reply: \"{reply}\"")
        if c.kind == "highlight" and c.anchor:
            lines.append(f"   highlighted text: \"{c.anchor}\"")
        if c.context and c.context != c.anchor:
            lines.append(f"   on the line: \"{c.context}\"")
        elif c.anchor and c.kind != "highlight":
            lines.append(f"   on the line: \"{c.anchor}\"")
    return "\n".join(lines)


def comments_source_batches(comments: list[PdfComment],
                            batch_size: int = 40,
                            pages: dict[int, str] | None = None) -> list[str]:
    """The comments split into batches, each formatted as its own corrections
    source. A marked-up proof can carry hundreds of comments; turning them all
    into edits in one model call overruns the output ceiling and truncates —
    silently losing every edit, since a half-finished structured reply parses as
    nothing. Reading a bounded batch at a time keeps each call small, fast and
    safe, and lets the caller show progress. Each batch re-states the framing
    header (via `comments_source`) so it stands alone; the per-batch numbering
    restarts, which is fine — the numbers are only the model's local reference."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    out = []
    for i in range(0, len(comments), batch_size):
        chunk = comments[i:i + batch_size]
        # Only the pages this batch's comments actually sit on — the whole book's
        # text in every batch would be most of a novel per call.
        wanted = {c.page for c in chunk}
        scoped = ({p: t for p, t in pages.items() if p in wanted}
                  if pages else None)
        out.append(comments_source(chunk, scoped))
    return out


def pdf_corrections_source(path: str | Path) -> tuple[str, list[PdfComment]]:
    """Read a commented PDF and return (source-text-for-the-extractor, comments).
    The text is empty when the PDF carries no comments."""
    comments = read_pdf_comments(path)
    return (comments_source(comments) if comments else ""), comments
