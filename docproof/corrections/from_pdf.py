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


@dataclass(frozen=True)
class PdfComment:
    """One correction read off the PDF: what the reviewer wrote, and the
    manuscript text it is anchored to."""
    page: int                 # 1-based
    instruction: str          # the comment text
    anchor: str               # the highlighted span, or the words nearest a note
    context: str              # the whole line(s) around the anchor
    kind: str                 # "highlight" | "note"


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
        words = _page_words(page)             # (x, y, text) in device space
        lines = _group_lines(words)
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
                anchor, context = _highlight_text(words, lines,
                                                  [float(v) for v in quad])
                kind = "highlight"
            else:
                rect = obj.get("/Rect")
                anchor, context = _note_text(
                    lines, float(rect[0]), float(rect[1])) if rect else ("", "")
                kind = "note"
            out.append(PdfComment(page=pi + 1, instruction=instruction,
                                  anchor=anchor.strip(), context=context.strip(),
                                  kind=kind))
    log.info("Read %d comment(s) from %s", len(out), Path(path).name)
    return out


def _page_words(page) -> list[tuple[float, float, str]]:
    """Text chunks with their device-space position. The visitor is handed the
    text matrix and the CTM separately; the on-page position is their product, so
    compose them — without it the coordinates do not line up with annotations."""
    chunks: list[tuple[float, float, str]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text and text.strip():
            x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
            y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
            chunks.append((x, y, text))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:                          # noqa: BLE001 - a page we can't read
        log.warning("Could not read text positions on a page", exc_info=True)
    return chunks


def _group_lines(words: list[tuple[float, float, str]]
                 ) -> list[tuple[float, str]]:
    """Chunks collapsed into text lines, each (y, text), sorted top to bottom.
    Chunks within ~a line height of each other share a line."""
    by_y: dict[int, list[tuple[float, str]]] = {}
    for x, y, text in words:
        key = round(y / 6) * 6                 # bucket to merge baseline jitter
        by_y.setdefault(key, []).append((x, text))
    lines = []
    for y, parts in by_y.items():
        parts.sort()
        text = " ".join(p for _, p in parts)
        lines.append((float(y), _norm(text)))
    lines.sort(key=lambda ly: -ly[0])          # top of page first
    return lines


def _highlight_text(words, lines, quad: list[float]) -> tuple[str, str]:
    """The text under a highlight's QuadPoints (the anchor), and the full line(s)
    it falls on (the context). QuadPoints come in eights: x1 y1 … x4 y4 per
    highlighted rectangle."""
    spans: list[str] = []
    ys: set[float] = set()
    for i in range(0, len(quad) - 7, 8):
        xs = quad[i:i + 8:2]
        yq = quad[i + 1:i + 8:2]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(yq), max(yq)
        inside = [(x, t) for x, y, t in words
                  if x0 - 2 <= x <= x1 + 2 and y0 - 2 <= y <= y1 + 2]
        inside.sort()
        if inside:
            spans.append(_norm(" ".join(t for _, t in inside)))
        ys.add((y0 + y1) / 2)
    context = " ".join(text for y, text in lines
                       if any(abs(y - qy) <= _LINE_TOLERANCE for qy in ys))
    return _norm(" ".join(spans)), _norm(context or " ".join(spans))


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
        "edit; the find must be copied verbatim from the anchored line.",
        "",
    ]
    for i, c in enumerate(comments, 1):
        lines.append(f"{i}. [page {c.page}] comment: \"{c.instruction}\"")
        if c.kind == "highlight" and c.anchor:
            lines.append(f"   highlighted text: \"{c.anchor}\"")
        if c.context and c.context != c.anchor:
            lines.append(f"   on the line: \"{c.context}\"")
        elif c.anchor and c.kind != "highlight":
            lines.append(f"   on the line: \"{c.anchor}\"")
    return "\n".join(lines)


def pdf_corrections_source(path: str | Path) -> tuple[str, list[PdfComment]]:
    """Read a commented PDF and return (source-text-for-the-extractor, comments).
    The text is empty when the PDF carries no comments."""
    comments = read_pdf_comments(path)
    return (comments_source(comments) if comments else ""), comments
