"""Reading a commented PDF proof into corrections.

An author or editor marks up a PDF of the book — sticky-note comments and
highlights, each carrying a correction. The comment text lives in the PDF's
annotation layer, not the rendered page; this pulls each one out together with
the manuscript text it points at — a highlight's own span (from its QuadPoints),
or the line a point-note sits on (matched by position) — so the model extractor
can turn the pair into one exact find/replace edit.

The reading here is deterministic (pypdf); the interpretation is the model's,
downstream in `extract`, and a person reviews the draft before anything is
applied. This is why a cryptic note ("iPhone", "make this a question") is still
useful: it arrives paired with the exact line it annotates, which is the context
the model (and the reviewer) needs.

pypdf only — no OCR. A scanned proof (an image with no text or annotation layer)
yields nothing here and is reported as such rather than guessed at.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("docproof.corrections.from_pdf")

# Annotation subtypes that carry a correction. A Popup is only the on-screen box
# for one of these and holds no content of its own, so it is ignored.
_COMMENT_SUBTYPES = {"/Text", "/Highlight", "/FreeText"}
# How near (in points) a point-note's anchor has to be to a line of text to be
# read as sitting on it. Lines are ~15pt apart; a note dropped between two lines
# still lands on the nearer one.
_LINE_TOLERANCE = 10.0
# Glyph-width defaults, in 1000ths of an em, for characters missing from a font's
# /Widths table: a slim space so word gaps are not overstated, and a mid default
# for anything else unmapped. Only relative widths within a line matter.
_SPACE_WIDTH = 250.0
_MEAN_WIDTH = 500.0
# Device points per 1000-em of width, used only when a line offers no measurable
# run to calibrate against (a single-chunk line). ~11pt body text.
_FALLBACK_SCALE = 0.006


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


def read_pdf_comments(path: str | Path) -> list[PdfComment]:
    """Every comment in the PDF, paired with the text it points at. Empty if the
    PDF carries no comment annotations (e.g. a plain or scanned proof)."""
    import pypdf

    reader = pypdf.PdfReader(str(path))
    out: list[PdfComment] = []
    for pi, page in enumerate(reader.pages):
        annots = page.get("/Annots")
        if not annots:
            continue
        words = _page_words(page)             # (x, y, text, glyph-widths)
        lines = _group_lines(words)
        scale = _page_scale(words)            # device points per em of glyph width
        for ref in annots:
            obj = ref.get_object()
            if str(obj.get("/Subtype")) not in _COMMENT_SUBTYPES:
                continue
            contents = obj.get("/Contents")
            if not contents:
                continue                      # a bare Popup / drawing, no comment
            instruction = str(contents).strip()
            if not instruction:
                continue
            quad = obj.get("/QuadPoints")
            if quad:
                anchor, context = _highlight_text(words, lines, scale,
                                                  [float(v) for v in quad])
                kind = "highlight"
            else:
                rect = obj.get("/Rect")
                anchor, context = _note_text(
                    lines, float(rect[0]), float(rect[1])) if rect else ("", "")
                kind = "note"
            page = pi + 1
            # A per-page running number keeps the id stable and legible; the read
            # order within a page is the annotation order pypdf yields.
            n = 1 + sum(1 for c in out if c.page == page)
            out.append(PdfComment(page=page, instruction=instruction,
                                  anchor=anchor.strip(), context=context.strip(),
                                  kind=kind, id=f"p{page}-{n}"))
    log.info("Read %d comment(s) from %s", len(out), Path(path).name)
    return out


def _page_words(page) -> list[tuple[float, float, str, tuple[float, ...]]]:
    """Text chunks with their device-space position and per-character glyph
    widths. The visitor is handed the text matrix and the CTM separately; the
    on-page position is their product, so compose them — without it the
    coordinates do not line up with annotations.

    Spaces between words arrive as their own `' '` chunks (pypdf represents a
    positioning jump that way), so they are kept, not stripped: they are the only
    reliable record of where one word ends and the next begins. A word is also
    sometimes split across chunks with no space between them (``un``|``nam``|``ed``
    for "unnamed"); keeping the real spaces and joining with nothing downstream is
    what tells those two cases apart. Only genuinely empty chunks are dropped.

    The width of each glyph (in 1000ths of an em, from the font's `/Widths`) is
    carried alongside so a highlight can be resolved to the exact characters it
    covers *inside* a chunk — pypdf emits a whole run of words at one position, so
    the run's start x alone cannot tell which word a mark sits on."""
    chunks: list[tuple[float, float, str, tuple[float, ...]]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text:
            x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
            y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
            chunks.append((x, y, text, _glyph_widths(text, font_dict)))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:                          # noqa: BLE001 - a page we can't read
        log.warning("Could not read text positions on a page", exc_info=True)
    return chunks


def _glyph_widths(text: str, font_dict) -> tuple[float, ...]:
    """Each character's advance width in 1000ths of an em, read from the font's
    `/Widths` array (indexed from `/FirstChar`). A code outside the array — a
    space, whose code sits below `/FirstChar`, or a smart quote from a different
    encoding — gets the font's mean width, except the space, which is given a
    slim default so a run's word gaps are not overstated. Absolute scale does not
    matter here: `_page_scale` derives the points-per-em from the page's own runs,
    so only the *relative* widths among glyphs are used."""
    try:
        widths = font_dict.get("/Widths") if font_dict else None
        first = int(font_dict.get("/FirstChar")) if widths is not None else 0
        table = [float(w) for w in widths] if widths else []
    except Exception:                          # noqa: BLE001 - odd font, fall back
        table = []
    if not table:
        return tuple(_SPACE_WIDTH if ch == " " else _MEAN_WIDTH for ch in text)
    mean = sum(table) / len(table)
    out = []
    for ch in text:
        i = ord(ch) - first
        if 0 <= i < len(table) and table[i]:
            out.append(table[i])
        else:
            out.append(_SPACE_WIDTH if ch == " " else mean)
    return tuple(out)


def _group_lines(words: list[tuple[float, float, str]]
                 ) -> list[tuple[float, str]]:
    """Chunks collapsed into text lines, each (y, text), sorted top to bottom.
    Chunks within ~a line height of each other share a line.

    Chunks are joined left to right with nothing between them: the inter-word
    spaces are already present as their own chunks, so concatenating reproduces
    the line verbatim. Joining with a space instead would both double the real
    spaces and invent spaces inside a word split across chunks ("un nam ed"); the
    trailing `_norm` then only has to fold the doubling a leading/inner run can
    leave, never to guess where a space belongs."""
    by_y: dict[int, list[tuple[float, str]]] = {}
    for x, y, text, _widths in words:
        key = round(y / 6) * 6                 # bucket to merge baseline jitter
        by_y.setdefault(key, []).append((x, text))
    lines = []
    for y, parts in by_y.items():
        parts.sort()
        text = "".join(p for _, p in parts)
        lines.append((float(y), _norm(text)))
    lines.sort(key=lambda ly: -ly[0])          # top of page first
    return lines


def _highlight_text(words, lines, scale: float,
                    quad: list[float]) -> tuple[str, str]:
    """The text under a highlight's QuadPoints (the anchor), and the full line(s)
    it falls on (the context). QuadPoints come in eights: x1 y1 … x4 y4 per
    highlighted rectangle.

    A word-by-word highlight is one rectangle per word; merged per line into a
    single box before reading, so a multi-word span comes back once and whole
    rather than each rectangle re-reading — and double-counting — the words its
    neighbour already snapped to."""
    boxes = []                                 # (x0, x1, y0, y1) per rectangle
    ys: set[float] = set()
    for i in range(0, len(quad) - 7, 8):
        xs = quad[i:i + 8:2]
        yq = quad[i + 1:i + 8:2]
        boxes.append([min(xs), max(xs), min(yq), max(yq)])
        ys.add((min(yq) + max(yq)) / 2)

    spans: list[str] = []
    for x0, x1, y0, y1 in _merge_boxes_by_line(boxes):
        span = _text_in_box(words, scale, x0, x1, y0, y1)
        if span:
            spans.append(_norm(span))
    context = " ".join(text for y, text in lines
                       if any(abs(y - qy) <= _LINE_TOLERANCE for qy in ys))
    anchor = _norm(" ".join(spans))
    return anchor, _norm(context or anchor)


def _merge_boxes_by_line(boxes: list[list[float]]) -> list[tuple]:
    """Rectangles that share a text line folded into one box spanning them all,
    top line first. Rectangles are on the same line when their vertical centres
    sit within a line height of each other."""
    merged: list[list[float]] = []
    for x0, x1, y0, y1 in sorted(boxes, key=lambda b: -(b[2] + b[3])):
        centre = (y0 + y1) / 2
        for m in merged:
            if abs((m[2] + m[3]) / 2 - centre) <= _LINE_TOLERANCE:
                m[0], m[1] = min(m[0], x0), max(m[1], x1)
                m[2], m[3] = min(m[2], y0), max(m[3], y1)
                break
        else:
            merged.append([x0, x1, y0, y1])
    return [tuple(m) for m in merged]


def _page_scale(words) -> float:
    """Device points per em of glyph width, for the page's body text.

    Every chunk's start x is exact, and every glyph's relative width is known, so
    one scale converts widths into on-page advances. It is read off the lines that
    carry more than one chunk — each neighbouring pair frames a measured advance
    over a known run of glyphs — and taken as the median, which shrugs off the
    outliers (a paragraph indent's fat space, a lone opening quote, the short
    tail of a ragged line). A single body size dominates a manuscript, so this one
    number places text on the many lines that are a single chunk and offer no
    internal measurement of their own."""
    by_y: dict[int, list[tuple[float, str, tuple[float, ...]]]] = {}
    for x, y, text, w in words:
        by_y.setdefault(round(y / 6) * 6, []).append((x, text, w))
    ratios: list[float] = []
    for parts in by_y.values():
        parts.sort()
        for k in range(len(parts) - 1):
            x, text, w = parts[k]
            em = sum(w)
            span = parts[k + 1][0] - x
            if text.strip() and em > 0 and span > 0:
                ratios.append(span / em)
    if not ratios:
        return _FALLBACK_SCALE
    ratios.sort()
    return ratios[len(ratios) // 2]


def _text_in_box(words, scale: float, x0: float, x1: float,
                 y0: float, y1: float) -> str:
    """The words a highlight rectangle covers, whole.

    A single chunk can carry several words (pypdf emits a whole run at one
    position), so a highlight over a word that is not first in its run shares no
    start-x with the box and would be missed if chunks were tested whole. Instead
    the mark's characters are found first — each glyph walked out from its run's
    exact start x by its own width times the page scale, so a proportional font's
    wide and narrow letters land where they render, and a glyph is kept when its
    midpoint lies in the box — then the run of them is widened at both ends to the
    nearest space.

    Snapping to whole words does two things at once: it repairs the clipping that
    width drift leaves at the ends of a long line ("rimson" becomes "crimson"),
    and it fills any small interior word the drift skipped, since the span between
    the first and last covered character is returned unbroken ("some of the", never
    "some the"). A word split across chunks ("un"|"nam"|"ed") is rejoined the same
    way — the pieces sit adjacent with no space between them."""
    band = sorted(((x, t, w) for x, y, t, w in words if y0 - 2 <= y <= y1 + 2),
                  key=lambda p: p[0])
    chars: list[str] = []
    inside: list[int] = []
    for x, t, w in band:
        cx = x
        for ch, cw in zip(t, w):
            centre = cx + cw * scale / 2
            if not ch.isspace() and x0 - 1 <= centre <= x1 + 1:
                inside.append(len(chars))
            chars.append(ch)
            cx += cw * scale
    if not inside:
        return ""
    lo, hi = min(inside), max(inside)
    while lo > 0 and not chars[lo - 1].isspace():
        lo -= 1
    while hi < len(chars) - 1 and not chars[hi + 1].isspace():
        hi += 1
    return "".join(chars[lo:hi + 1])


def _note_text(lines, rx: float, ry: float) -> tuple[str, str]:
    """The line a point-note sits on: anchor and context are the same line, since
    a sticky note carries no span.

    A note is anchored just below the baseline of the line it marks (its icon
    hangs under the word), so the line it belongs to is the nearest one *at or
    above* the anchor point — not simply the nearest, which would drift to the
    line below whenever the note sits between two. Falls back to the plain
    nearest line if the note is above all the text on the page."""
    if not lines:
        return "", ""
    above = [ly for ly in lines if ly[0] >= ry - 2.0]
    y, text = (min(above, key=lambda ly: ly[0] - ry) if above
               else min(lines, key=lambda ly: abs(ly[0] - ry)))
    return text, text


def _norm(s: str) -> str:
    return " ".join(s.split())


def comments_source(comments: list[PdfComment]) -> str:
    """Format the comments as the corrections source the model extractor reads:
    each comment beside the exact manuscript text it annotates, so the model can
    quote a real anchor rather than guess one."""
    lines = [
        "Corrections marked on a PDF proof of a book. Each item is a reviewer's "
        "comment and the manuscript text it points at — a highlighted span, or "
        "the line a sticky note sits on. Turn each into one exact find/replace "
        "edit; the find must be copied verbatim from the anchored line. Each item "
        "carries an id (e.g. p3-2); set the edit's `source` to that id so every "
        "comment can be accounted for. One edit per item — never drop one.",
        "",
    ]
    for i, c in enumerate(comments, 1):
        cid = c.id or f"c{i}"
        lines.append(f"{i}. (id {cid}) [page {c.page}] comment: \"{c.instruction}\"")
        if c.kind == "highlight" and c.anchor:
            lines.append(f"   highlighted text: \"{c.anchor}\"")
        if c.context and c.context != c.anchor:
            lines.append(f"   on the line: \"{c.context}\"")
        elif c.anchor and c.kind != "highlight":
            lines.append(f"   on the line: \"{c.anchor}\"")
    return "\n".join(lines)


def comments_source_batches(comments: list[PdfComment],
                            batch_size: int = 40) -> list[str]:
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
    return [comments_source(comments[i:i + batch_size])
            for i in range(0, len(comments), batch_size)]


def pdf_corrections_source(path: str | Path) -> tuple[str, list[PdfComment]]:
    """Read a commented PDF and return (source-text-for-the-extractor, comments).
    The text is empty when the PDF carries no comments."""
    comments = read_pdf_comments(path)
    return (comments_source(comments) if comments else ""), comments
