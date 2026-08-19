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

# Character formatting an edit can ask for, and the IDML attributes that express it.
# "Italicize this" is not a layout request: it is a property of a run of text, which
# an IDML carries on the CharacterStyleRange around it — so it is appliable, and
# routing it to a designer was leaving real work undone on every proof. "roman"
# clears a local italic rather than writing a second override over it.
FORMAT_ITALIC = "italic"
FORMAT_ROMAN = "roman"
# A swash is the same kind of thing one step further out: not a different face but
# an alternate glyph the face already carries, switched on by an OpenType feature.
# InDesign writes that as `OTFSwash` on the character range, so "swoop the R" is a
# character property exactly as italic is — appliable, not a note for a designer.
# The feature only substitutes where the font has a swash form, so applying it to
# the marked word is what a designer does by hand: the letters without one are
# left alone.
FORMAT_SWASH = "swash"
FORMAT_NO_SWASH = "no-swash"
FORMATS: dict[str, dict[str, str | None]] = {
    FORMAT_ITALIC: {"FontStyle": "Italic"},
    FORMAT_ROMAN: {"FontStyle": None},
    FORMAT_SWASH: {"OTFSwash": "true"},
    FORMAT_NO_SWASH: {"OTFSwash": None},
}

# The other way an IDML says "italic": a character style the designer applied,
# rather than a local override on the range. Which one a book uses is the book's
# choice — this one uses its `Italic` style 117 times — and a correction has to
# read what is there rather than assume. Clearing a `FontStyle` that was never
# set is what left every "de-italicize this song title" reported as applied and
# visibly unchanged in the file. `apply._format_attrs` resolves the pair against
# the span being styled.
NO_CHARACTER_STYLE = "CharacterStyle/$ID/[No character style]"
# The style names that mean italic. InDesign's default is "Italic"; a designer
# who renamed it usually says so in the name.
_ITALIC_NAMES = ("italic", "italics", "oblique")


def style_name(applied: str) -> str:
    """The bare name of an `AppliedCharacterStyle` — "Italic" for
    "CharacterStyle/Italic"."""
    return (applied or "").rsplit("/", 1)[-1].strip()


def is_italic_style(applied: str) -> bool:
    """Whether an applied character style is one that sets italics."""
    return style_name(applied).lower() in _ITALIC_NAMES


def is_plain_style(applied: str) -> bool:
    """Whether a range carries no character style of its own — the case where a
    correction may set one without taking anything out."""
    name = style_name(applied)
    return not name or name == "[No character style]"


# What a reviewer can ask of a whole paragraph, and the IDML range attributes that
# say it. These are the requests that read as layout and are not: where a chapter
# starts, whether a heading may be left stranded at the foot of a page, and how much
# air sits above a paragraph are all properties of the paragraph, carried in the
# story — not geometry in a spread. An attribute set to None is removed, so a forced
# break can be cleared as well as set.
PARA_ATTRS: dict[str, dict[str, str | None]] = {
    "page-break":     {"StartParagraph": "NextPage"},
    "recto":          {"StartParagraph": "NextOddPage"},
    "verso":          {"StartParagraph": "NextEvenPage"},
    "column-break":   {"StartParagraph": "NextColumn"},
    "frame-break":    {"StartParagraph": "NextFrame"},
    "no-page-break":  {"StartParagraph": None},
    "keep-with-next": {"KeepWithNext": "1"},
    "keep-together":  {"KeepAllLinesTogether": "true"},
    "allow-break":    {"KeepAllLinesTogether": "false"},
}
# Operations that change how many paragraphs the story has. They are applied after
# every other edit, because they move the paragraph indices under anything that has
# not run yet — and each re-locates its own anchor by text, so running last costs
# them nothing.
PARA_DELETE = "delete-paragraph"
PARA_INSERT_AFTER = "insert-after"
PARA_INSERT_BEFORE = "insert-before"
# The two that change where the breaks are rather than how many paragraphs carry
# text. "Delete the line break between X and Y" and "start a new paragraph here"
# are the commonest requests on a proof of verse, where every line is its own
# paragraph and a reviewer's sentence runs across several of them. Both are
# anchored by text like any other edit; `merge-next` joins the paragraph the anchor
# lands in with the one after it, `split-at` breaks it immediately before the
# anchor.
PARA_MERGE_NEXT = "merge-next"
PARA_SPLIT_AT = "split-at"
PARA_STRUCTURAL = (PARA_DELETE, PARA_INSERT_AFTER, PARA_INSERT_BEFORE,
                   PARA_MERGE_NEXT, PARA_SPLIT_AT)
PARA_OPS = tuple(PARA_ATTRS) + PARA_STRUCTURAL


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
    # The 1-based page of the proof the correction was marked on; 0 when unknown
    # (a typed list). An IDML has no pages, so this is not a location on its own —
    # a page map (see `pagemap`) turns it into the run of book text that page set,
    # and `apply` then narrows a repeated `find` to that run. This is what stops a
    # mark on "," having six thousand places in the book to land.
    page: int = 0
    # Character formatting to apply to `find` instead of rewriting it — one of
    # `FORMATS`. The text is untouched, so `replace` equals `find` on such an edit
    # and the usual "find == replace, nothing to do" shortcut must not swallow it.
    format: str = ""
    # A whole-paragraph operation from `PARA_OPS` — a forced break, a keep, or a
    # paragraph inserted or removed. `find` still anchors it: the paragraph acted on
    # is the one the anchor lands in, so a layout request is located exactly as a
    # word swap is, and refused the same way when it cannot be.
    paragraph: str = ""
    # The paragraph style to apply to that paragraph, e.g.
    # "ParagraphStyle/Block Quote". Named in full because the style has to exist in
    # the book already — this reassigns a paragraph to a design, it does not invent
    # one.
    paragraph_style: str = ""
    # What a model concluded about a query it would not answer for the reviewer —
    # the evidence and the reading, written for the person who now owns it. This
    # changes nothing about how the edit is applied: an edit carrying advice is
    # still the query it was, still flagged, still a human's. It only means the
    # flag arrives with the work done, so resolving it is a glance rather than an
    # investigation.
    advice: str = ""

    @property
    def is_structural(self) -> bool:
        """Whether this edit changes how many paragraphs the story has."""
        return self.paragraph in PARA_STRUCTURAL

    @property
    def is_layout(self) -> bool:
        """Whether this edit is about the paragraph rather than its words."""
        return bool(self.paragraph or self.paragraph_style)

    @property
    def is_deletion(self) -> bool:
        return self.replace == "" and self.find != ""

    @property
    def is_format(self) -> bool:
        """Whether this edit styles its span rather than rewriting it."""
        return bool(self.format)


# Outcome statuses for a single edit.
APPLIED = "applied"
NOT_FOUND = "not_found"             # the find text is nowhere in the document
AMBIGUOUS = "ambiguous"            # it appears more than once and no occurrence given
CROSSES_PARAGRAPH = "crosses_paragraph"   # the span straddles a paragraph break
NO_CHANGE = "no_change"            # find == replace, nothing to do
ROUTED_TO_DESIGN = "routed_to_design"     # a design request, not a text edit
OVERLAPS = "overlaps"              # its span collides with an edit already applied
WITHHELD = "withheld"             # the sanity gate held it back for a human
UNSTYLEABLE = "unstyleable"       # the span's text is not held by a character range
UNPLACEABLE = "unplaceable"       # the paragraph could not be given a range of its own


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
    # On an OVERLAPS outcome: the id(s) of the applied edit(s) whose span this
    # one collided with — what lets the flag name the other correction, and lets
    # the second look put the two side by side and merge them into one.
    collides_with: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        return self.status == APPLIED

    @property
    def needs_human(self) -> bool:
        """The edit did not land cleanly and someone has to look."""
        return self.status in (NOT_FOUND, AMBIGUOUS, CROSSES_PARAGRAPH,
                               ROUTED_TO_DESIGN, OVERLAPS, WITHHELD,
                               UNSTYLEABLE, UNPLACEABLE)


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


@dataclass(frozen=True)
class CheckItem:
    """One thing a person has to look at in InDesign, because no file comparison can
    settle it.

    Whether a line breaks well, whether a heading is stranded, whether a page runs
    long — each is a result of *composition*, and composing is InDesign's job. This
    engine can make the change and can prove the text is what the list asked for; it
    cannot see the page. So instead of claiming an outcome it cannot check, it hands
    over a located list: the page the mark was on, the paragraph it landed in, what
    to look at and why.

    `edit_ids` names the corrections that caused it, when the check follows work
    this run did; a check can also stand alone, for a reviewer note that was never a
    text edit at all."""
    page: int                          # the proof page, 0 when unknown
    story_id: str
    paragraph: int
    what: str                          # what to look at
    why: str                           # the reviewer's note, or what was applied
    edit_ids: tuple[str, ...] = ()


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
    # The character formatting applied here, when that is what changed. A formatting
    # edit rewrites no text, so the before and after lines read identically — and
    # without this the row would look like a change that did not happen, or be
    # dropped as one. It is still the row a designer needs: the italics have to be
    # confirmed to have landed on the right words.
    formatting: str = ""


@dataclass(frozen=True)
class VerifyReport:
    """Before vs after, reconciled against the edit list."""
    reconciliations: tuple[Reconciliation, ...]
    discrepancies: tuple[Discrepancy, ...]
    paragraphs_before: int = 0
    paragraphs_after: int = 0
    # What a clean apply of the list *should* leave. Usually the same as `before` —
    # a correction rewrites words, it does not add or remove paragraphs. But some
    # corrections do exactly that on purpose (a deleted Acknowledgments page put
    # back, a stray blank line taken out), and the alarm has to be able to tell an
    # asked-for change from the paragraph merge a hand-run script produces. So the
    # comparison that matters is expected against actual; before against after is
    # reported alongside, as information.
    paragraphs_expected: int = 0
    # One entry per paragraph an applied edit changed, before and after, for a
    # human to read down and confirm. Empty in the rare run that applied nothing.
    changes: tuple[ReviewChange, ...] = ()

    @property
    def clean(self) -> bool:
        """Every edit landed exactly and nothing else changed."""
        return (not self.discrepancies
                and all(r.status == APPLIED_EXACTLY for r in self.reconciliations)
                and not self.structure_changed)

    @property
    def structure_changed(self) -> bool:
        """The file has a different number of paragraphs than the corrections asked
        for — a merge, a split, a line lost. An intended insertion or deletion is
        not this: it is in the expectation too, so the two agree."""
        return self.paragraphs_expected != self.paragraphs_after

    @property
    def structure_intended(self) -> bool:
        """The corrections deliberately changed how many paragraphs there are."""
        return self.paragraphs_expected != self.paragraphs_before
