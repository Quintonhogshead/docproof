"""Reading an author's tracked-changes .docx into an `Edit` list.

The most reliable corrections source there is: a redlined Word file states the
before and the after explicitly (``w:ins`` / ``w:del``), so no model is needed to
interpret it — the edit list is read straight off the markup. Each changed
paragraph becomes one anchored edit whose `find` is the minimal changed span,
widened by a little surrounding context so it locates uniquely in the book.

This reuses the review pipeline's own tracked-changes reader (`paragraph_view_text`
gives the accept/reject views) and its minimal-diff helper (`validator.shrink`),
so a redline read here is read exactly the way the rest of DocProof reads one.
Nothing is guessed: a paragraph whose 'before' text the finished book does not
contain is not forced — `apply` flags it downstream, the same as any other edit.
"""
from __future__ import annotations

from pathlib import Path

from ..reassembler import paragraph_view_text
from ..utils.xml_helpers import DocxPackage, walk_package
from ..validator import shrink
from .model import Edit, MECHANICAL
from .parse import ParseIssue, ParseResult

# How far to widen the anchor beyond the minimal changed span, on each side, so a
# one-word fix does not anchor on a word so common it is ambiguous — snapped out
# to whole words. A pure insertion has an empty changed span, so this context is
# the only thing anchoring it.
CONTEXT_CHARS = 24


def edits_from_docx(path: str | Path) -> ParseResult:
    """Read every tracked change in a .docx into a `ParseResult`.

    One edit per changed paragraph; unchanged paragraphs are skipped. A change
    with no anchorable text (a wholly new paragraph, which has no 'before' to
    locate) becomes a `ParseIssue` rather than a silent drop."""
    pkg = DocxPackage(Path(path))
    edits: list[Edit] = []
    issues: list[ParseIssue] = []
    n = 0
    for wp in walk_package(pkg):
        before = paragraph_view_text(wp.element, "reject")
        after = paragraph_view_text(wp.element, "accept")
        if before == after:
            continue                       # nothing tracked in this paragraph
        n += 1
        outcome = _anchored_edit(f"w{n}", before, after, index=n - 1)
        (issues if isinstance(outcome, ParseIssue) else edits).append(outcome)
    return ParseResult(edits=tuple(edits), issues=tuple(issues))


def text_from_docx(path: str | Path) -> str:
    """The Word file's plain text, one paragraph per line.

    Not every corrections .docx is a redline: an editor or a proofreader as
    often sends a *list* — "p. 12, change 'teh' to 'the'" — typed in Word. That
    file has no tracked changes to read off, so it is read as text and put to
    the same model that reads a pasted list (see `corrections.extract`). Any
    tracked changes in it are taken as accepted, which is what the list-writer
    meant by leaving them in."""
    pkg = DocxPackage(Path(path))
    lines = [paragraph_view_text(wp.element, "accept") for wp in walk_package(pkg)]
    return "\n".join(line for line in lines if line.strip())


def _anchored_edit(eid: str, before: str, after: str, *, index: int):
    """Turn one paragraph's before/after into an anchored `Edit`, or a
    `ParseIssue` if it cannot be anchored (nothing in the 'before' to find)."""
    prefix_len, deleted, inserted = shrink(before, after)
    start, end = prefix_len, prefix_len + len(deleted)
    lo = _word_left(before, start)
    hi = _word_right(before, end)
    ctx_before, ctx_after = before[lo:start], before[end:hi]
    find = ctx_before + deleted + ctx_after
    replace = ctx_before + inserted + ctx_after
    if not find.strip():
        # A wholly-inserted paragraph (or one whose only change is whitespace at
        # a bare paragraph) has no text to anchor to — an insertion the anchor
        # cannot place. Flag it for a human, do not guess a location.
        return ParseIssue(
            index, "a tracked change with no surrounding text to anchor to "
            "(a newly inserted paragraph can't be placed by find/replace)",
            {"before": before, "after": after})
    return Edit(id=eid, find=find, replace=replace, kind=MECHANICAL)


def _word_left(s: str, pos: int) -> int:
    """The start of the context window left of `pos`: back up to CONTEXT_CHARS,
    then out to a word boundary so the anchor never begins mid-word."""
    lo = max(0, pos - CONTEXT_CHARS)
    while lo > 0 and not s[lo - 1].isspace():
        lo -= 1
    return lo


def _word_right(s: str, pos: int) -> int:
    hi = min(len(s), pos + CONTEXT_CHARS)
    while hi < len(s) and not s[hi].isspace():
        hi += 1
    return hi
