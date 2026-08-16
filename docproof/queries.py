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


def shows_margin_comment(f: Finding, *, query_comments: bool,
                         not_applied_comments: bool) -> bool:
    """Whether a not-applied finding leaves a margin comment on the delivered
    document, or lives only in the change log, summary.md and findings.json.

    Two kinds reach the margin, and they are treated differently:

    * A genuine query — a question from an asking pass (continuity, fact-check,
      consistency, an unconverted number-rule site) — always leaves one. Asking
      is its only channel; there is nothing else for it to be.

    * A correction the tool chose not to make — an edit a judge gate or the
      verifier withdrew (`withheld`), a below-gate catch, an oversized fix —
      leaves one only when `not_applied_comments` is on. Off by default, so the
      document carries the changes and the real questions, not a commentary on
      what was declined; the change log still records every one. When it is on,
      `query_comments` still decides the below-gate and oversized ones, so
      turning it on restores the prior behaviour exactly."""
    if f.status == "query" and not f.withheld:
        return True                          # genuine asking-pass query: always
    if not not_applied_comments:
        return False                         # declined edits: log-only by default
    if f.status == "query":                  # a judge/verifier-withheld edit
        return True
    return (f.status in ("skipped_low_confidence", "rejected_oversized")
            and query_comments)
