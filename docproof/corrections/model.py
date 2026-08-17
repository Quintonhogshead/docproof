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
                               ROUTED_TO_DESIGN)


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
class VerifyReport:
    """Before vs after, reconciled against the edit list."""
    reconciliations: tuple[Reconciliation, ...]
    discrepancies: tuple[Discrepancy, ...]
    paragraphs_before: int = 0
    paragraphs_after: int = 0

    @property
    def clean(self) -> bool:
        """Every edit landed exactly and nothing else changed."""
        return (not self.discrepancies
                and all(r.status == APPLIED_EXACTLY for r in self.reconciliations)
                and self.paragraphs_before == self.paragraphs_after)

    @property
    def structure_changed(self) -> bool:
        return self.paragraphs_before != self.paragraphs_after
