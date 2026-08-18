"""What the corrections engine passes between its stages.

An `Edit` is one correction — find this text, make it read that way. It is what
a parser produces and what `apply` consumes; nothing here knows where it came
from (a typed list, a tracked-changes PDF), which is the whole point of keeping
the deterministic core free of the parsing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# How confident the source is that this is a mechanical, safe-to-apply change.
MECHANICAL = "mechanical"     # a text swap with one right answer
JUDGMENT = "judgment"         # needs a human eye even if it anchors
DESIGN = "design"             # a layout request — not a text edit at all


@dataclass(frozen=True)
class Edit:
    """One correction, anchored by the verbatim text it changes.

    `find` is the exact run of text to locate; `replace` is what it becomes. A
    pure deletion has `replace == ""`; a pure insertion has `find == ""` and is
    not yet supported by the anchor (there is nothing to locate), so parsers
    express an insertion as a replace of the surrounding text. `occurrence`
    disambiguates a `find` that appears more than once — 1-based, and 0 means
    "there must be exactly one, anywhere"."""
    id: str
    find: str
    replace: str
    instruction: str = ""              # the human note, for the report
    kind: str = MECHANICAL
    occurrence: int = 0                # 0 = require uniqueness; N = the Nth
    # An optional verbatim anchor that must contain `find`. When given, `find` is
    # located *inside* the context rather than across the whole book, so a common
    # word ("harbor") lands on the intended instance — the one whose surrounding
    # line the correction was attached to. This is how a page-anchored correction
    # is scoped: an IDML has no pages, but the line the mark sat on is text, and
    # text is what the book has. `occurrence` then disambiguates the context, not
    # the find.
    context: str = ""
    # The id of the source comment this edit was read from (a PdfComment.id), so
    # the finished change log can prove every reviewer comment reached an outcome
    # — applied, flagged, or nothing at all. Empty for a typed list with no
    # comment behind it, or an edit a reviewer added by hand during review.
    source: str = ""

    @property
    def is_deletion(self) -> bool:
        return self.replace == "" and self.find != ""


# Outcome statuses for a single edit.
APPLIED = "applied"
NOT_FOUND = "not_found"             # the find text is nowhere in the document
AMBIGUOUS = "ambiguous"            # it appears more than once and no occurrence given
CROSSES_PARAGRAPH = "crosses_paragraph"   # the span straddles a paragraph break
NO_CHANGE = "no_change"            # find == replace, nothing to do
ROUTED_TO_DESIGN = "routed_to_design"     # a design request, not a text edit
OVERLAPS = "overlaps"              # its span collides with an edit already applied
WITHHELD = "withheld"             # the sanity gate held it back for a human


@dataclass(frozen=True)
class EditOutcome:
    """What became of one edit. Applied edits carry where they landed so the
    verifier and the change log can point at them."""
    edit: Edit
    status: str
    story_id: str = ""
    paragraph: int = -1
    occurrences: int = 0               # how many times `find` was found in all
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status == APPLIED

    @property
    def needs_human(self) -> bool:
        """The edit did not land cleanly and someone has to look."""
        return self.status in (NOT_FOUND, AMBIGUOUS, CROSSES_PARAGRAPH,
                               ROUTED_TO_DESIGN, OVERLAPS, WITHHELD)


@dataclass(frozen=True)
class ApplyReport:
    """The result of applying an edit list to one IDML."""
    outcomes: tuple[EditOutcome, ...]
    stories_changed: tuple[str, ...] = ()

    @property
    def applied(self) -> int:
        return sum(1 for o in self.outcomes if o.applied)

    @property
    def flagged(self) -> tuple[EditOutcome, ...]:
        return tuple(o for o in self.outcomes if o.needs_human)

    def summary(self) -> str:
        n = len(self.outcomes)
        return (f"{self.applied}/{n} applied, {len(self.flagged)} for a human "
                f"({n - self.applied - len(self.flagged)} no-op)")


# --- the reviewer-comment ledger ---------------------------------------------
# Every comment read off the proof ends at exactly one of these, so none is ever
# swallowed: it either became an edit (applied / flagged / no-op) or the model
# turned it into nothing (not_extracted) — and the last is the one that used to
# vanish. A comment reaches `flagged` or `not_extracted` when a human still owns
# it; `applied` and `no_op` are done.
DISP_APPLIED = "applied"
DISP_FLAGGED = "flagged"            # its edit could not land — a human owns it
DISP_NO_OP = "no_op"               # its edit was a no-op (find == replace)
DISP_NOT_EXTRACTED = "not_extracted"   # no edit was made for it at all

# The dispositions that still need a person's eye — the change log's "needs a
# human" bucket, and the count the card shows.
DISP_NEEDS_HUMAN = (DISP_FLAGGED, DISP_NOT_EXTRACTED)


@dataclass(frozen=True)
class CommentDisposition:
    """What became of one reviewer comment: its verbatim text and where it points
    (so an editor can act on an un-appliable one straight from the change log),
    the disposition it reached, and the ids of the edit(s) it produced."""
    id: str
    page: int
    kind: str                          # "highlight" | "note" | "" (non-PDF source)
    instruction: str                   # the reviewer's comment, verbatim
    anchor: str                        # the text it was attached to
    disposition: str                   # one of the DISP_* above
    edit_ids: tuple[str, ...] = ()
    detail: str = ""                   # why, when flagged (from the edit outcome)

    @property
    def needs_human(self) -> bool:
        return self.disposition in DISP_NEEDS_HUMAN


# --- verification -------------------------------------------------------------

APPLIED_EXACTLY = "applied_exactly"
DEVIATES = "deviates"              # the change was made but not as asked
MISSING = "missing"               # the requested change is not in the document


@dataclass(frozen=True)
class Reconciliation:
    """One edit, checked against what the document actually now says."""
    edit: Edit
    status: str                       # APPLIED_EXACTLY | DEVIATES | MISSING
    detail: str = ""


@dataclass(frozen=True)
class Discrepancy:
    """A change found in the document that no edit asked for — the collateral
    damage the verifier exists to catch. Located by paragraph so a human can
    find it fast."""
    story_id: str
    paragraph: int
    before: str
    after: str


@dataclass(frozen=True)
class ReviewChange:
    """One paragraph an applied edit changed, kept as the whole line before and
    after — so a designer can read the correction *in context* and confirm it is
    right, not merely that it landed. This is the other half of verification: the
    discrepancy list proves nothing changed that shouldn't have; this proves what
    did change reads correctly. A line two edits touched appears once, fully
    corrected, carrying both their ids and notes."""
    story_id: str
    paragraph: int
    before: str
    after: str
    edit_ids: tuple[str, ...] = ()
    instruction: str = ""              # the reviewer note(s), when the edits had any


@dataclass(frozen=True)
class VerifyReport:
    """Before vs after, reconciled against the edit list."""
    reconciliations: tuple[Reconciliation, ...]
    discrepancies: tuple[Discrepancy, ...]
    paragraphs_before: int = 0
    paragraphs_after: int = 0
    # One entry per paragraph an applied edit changed, before and after, for a
    # human to read down and confirm. Empty in the rare run that applied nothing.
    changes: tuple[ReviewChange, ...] = ()

    @property
    def clean(self) -> bool:
        """Every edit landed exactly and nothing else changed."""
        return (not self.discrepancies
                and all(r.status == APPLIED_EXACTLY for r in self.reconciliations)
                and self.paragraphs_before == self.paragraphs_after)

    @property
    def structure_changed(self) -> bool:
        return self.paragraphs_before != self.paragraphs_after
