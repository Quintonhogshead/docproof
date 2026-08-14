"""What a query points at, and what it says.

The query channel is one of the two the brief draws hardest (see
docs/deliverables.md), and neither half of it is format-specific: which span a
question is about, and the words it asks in, are the same whether the answer
ends up as a Word comment or an InDesign Note. They lived in the .docx
reassembler until IDML needed them too — and an IDML review that quietly
dropped its queries is exactly what one copy per format buys you.

The formats keep only the anchoring: a Word comment spans a range, an InDesign
Note is a point.
"""
from __future__ import annotations

from .models import Finding


def query_span(f: Finding, para_text: str) -> tuple[int, int]:
    """What a query should point at: the sentence it is asking about, not the
    characters an edit would have touched.

    A below-gate finding carries the shrunk anchor of the change that was not
    made, which for a comma splice is one comma. A comment hanging off a
    single comma tells the author nothing about what is being questioned.

    Re-search with the validator's fold-tolerant anchor, not a bare exact
    match: the validator admits a finding whose original_text renders the
    manuscript's curly punctuation as straight, so an exact re-search here
    would miss it and fall back to the shrunk one-character diff span — the
    very thing this function exists to avoid."""
    from .validator import anchor_offset
    s = anchor_offset(para_text, f.original_text, f.occurrence)
    if s == -1:                                  # unanchorable text never
        return f.anchor.start, f.anchor.end      # reaches here past the validator
    return s, s + len(f.original_text)


def query_text(f: Finding) -> str:
    """What a query says in the margin. A question has to read as one — an
    author who cannot tell a query from a correction has lost the distinction
    the two channels exist to draw."""
    if f.status == "query":
        return f.explanation or f"{f.error_type.replace('_', ' ')}: worth a look."
    kind = f.error_type.replace("_", " ")
    if f.status == "rejected_oversized":
        # A real catch whose fix rewrites more than a minimal edit should — a
        # run-on split in two, a restructured list. Not applied automatically,
        # but the author should still see it and make the change by hand.
        parts = [f"Possibly {kind} — the suggested fix was too large to apply "
                 f"as a minimal tracked change, so it is left for you to make "
                 f"by hand."]
    else:
        parts = [f"Possibly {kind} — left as written, because it may be deliberate."]
    if f.explanation:
        parts.append(f.explanation)
    if f.corrected_text and f.corrected_text != f.original_text:
        parts.append(f"Suggested: {f.corrected_text}")
    return " ".join(parts)


def wanted_statuses(query_comments: bool) -> set[str]:
    """Which findings take the query channel.

    A query-only error type always does — asking is its whole output, so there
    is no other channel for it to fall back to, and it is written even with
    comments otherwise off. Below-gate and oversized findings join it when
    `query_comments` is on: the model thought something was wrong but not
    confidently enough to touch it, which is what a margin question is for, and
    an oversized fix is a real catch whose repair is too large to auto-apply.
    Surfacing either beats dropping the information silently."""
    wanted = {"query"}
    if query_comments:
        wanted.add("skipped_low_confidence")
        wanted.add("rejected_oversized")
    return wanted
