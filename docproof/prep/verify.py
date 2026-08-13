"""The gate: the author's words, before and after, compared word for word.

The house prompt's first rule is that prep changes no word and no punctuation
character of the manuscript. That is not a thing to hope a model respected — it
is a thing to check, on the file that was actually written, and to refuse to
ship when it fails.

The tracked file is checked twice: accepting every change must give the clean
text, and rejecting every change must give the manuscript back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..reassembler import paragraph_view_text
from ..utils.xml_helpers import DocxPackage, paragraph_text, walk_package
from .ingest import BODY_PART
from .model import Structure

log = logging.getLogger("docproof.prep.verify")


class VerificationFailed(Exception):
    """The output does not say what the manuscript said. Nothing ships."""


@dataclass(frozen=True)
class Verification:
    view: str            # "clean" | "accept" | "reject"
    ok: bool
    source_words: int
    output_words: int
    detail: str

    def describe(self) -> str:
        if self.ok:
            return (f"{self.view}: {self.output_words:,} words, identical to "
                    f"the manuscript")
        return (f"{self.view}: {self.output_words:,} words against the "
                f"manuscript's {self.source_words:,} — {self.detail}")


def stream(texts, glyph: str) -> list[str]:
    """The author's words, in order, as bare whitespace-separated tokens.

    Line breaks, paragraph boundaries and runs of spaces are not the author's
    words, so they are not compared — this is a check on the text, and prep is
    allowed to remove a blank line. Scene-break glyphs are dropped from both
    sides: an inserted one is the single change the spec permits, and dropping
    the token by value keeps a break the author typed themselves from being
    counted on one side only.

    A glyph is dropped token by token, because a house set is as likely to
    write a break as "* * *" as "***" — one of those is three words to anything
    counting words, and counting an inserted break as three new words would
    fail every manuscript that has one."""
    skip = set(glyph.split())
    out: list[str] = []
    for text in texts:
        for token in text.split():
            if token not in skip:
                out.append(token)
    return out


def source_stream(structure: Structure, glyph: str) -> list[str]:
    return stream((p.text for p in structure.paragraphs), glyph)


def output_stream(path: str | Path, glyph: str, *, view: str = "clean") -> list[str]:
    """Read a written file back and extract the same thing.

    Deliberately re-opened from disk rather than read out of the package still
    in memory: what matters is what the file says, including anything the save
    itself might have done to it.

    A drop cap is the one place the book writer moves text between paragraphs:
    the opening letter sits in its own frame paragraph (`w:framePr` with
    `w:dropCap`), exactly as Word records its own. The letter is glued back
    onto the paragraph that follows, so "M" + "om had been dead" reads as the
    word the author wrote."""
    pkg = DocxPackage(Path(path))
    texts: list[str] = []
    held = ""
    for wp in walk_package(pkg):
        if wp.part != BODY_PART:
            continue
        text = (paragraph_text(wp.element) if view == "clean"
                else paragraph_view_text(wp.element, view))
        if _is_drop_cap(wp.element):
            held += text
            continue
        texts.append(held + text)
        held = ""
    if held:
        texts.append(held)
    return stream(texts, glyph)


def _is_drop_cap(p) -> bool:
    from ..utils.xml_helpers import qn

    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        return False
    frame = ppr.find(qn("w:framePr"))
    return frame is not None and frame.get(qn("w:dropCap")) is not None


def compare(source: list[str], output: list[str], *, view: str) -> Verification:
    if source == output:
        return Verification(view=view, ok=True, source_words=len(source),
                            output_words=len(output), detail="identical")
    detail = _first_divergence(source, output)
    return Verification(view=view, ok=False, source_words=len(source),
                        output_words=len(output), detail=detail)


def _first_divergence(source: list[str], output: list[str]) -> str:
    limit = min(len(source), len(output))
    for i in range(limit):
        if source[i] != output[i]:
            context = " ".join(source[max(0, i - 6):i])
            return (f"they part at word {i + 1}: the manuscript has "
                    f"{source[i]!r}, the output has {output[i]!r} "
                    f"(after “…{context}”)")
    if len(output) > len(source):
        return (f"the output has {len(output) - len(source)} extra word(s) at "
                f"the end, starting {output[limit]!r}")
    return (f"the output stops {len(source) - len(output)} word(s) early; the "
            f"manuscript continues {source[limit]!r}")


def verify_output(structure: Structure, path: str | Path, glyph: str, *,
                  views: tuple[str, ...] = ("clean",)) -> list[Verification]:
    """Check one written file against the manuscript it came from.

    Raises rather than returning a bad result: a file whose text has drifted is
    not something to hand over with a warning attached."""
    source = source_stream(structure, glyph)
    results = [compare(source, output_stream(path, glyph, view=view), view=view)
               for view in views]
    for result in results:
        if result.ok:
            log.info("Verified %s — %s", Path(path).name, result.describe())
        else:
            log.error("Verification failed for %s — %s", Path(path).name,
                      result.describe())
    bad = [r for r in results if not r.ok]
    if bad:
        raise VerificationFailed(
            f"{Path(path).name} does not match the author's text and was not "
            f"kept. " + "; ".join(r.describe() for r in bad))
    return results
