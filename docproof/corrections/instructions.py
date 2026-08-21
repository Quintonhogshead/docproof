"""Reviewer notes that are a function of the text they are attached to.

Most of what a copy editor writes on a proof is not a request to be interpreted.
"Lowercase" against a highlighted word means that word in lower case. "Replace
comma with period" against a highlighted clause means that clause's comma is a
full stop. "wouldn't" against "wouldnt" means exactly what it says. Each is one
right answer, computable from the note and the marked span, with nothing to
decide.

Those notes were nonetheless being handed to a model, and the model had to invent
a `find` for them — from a PDF rendering of a book it had never been shown. It
routinely produced a bare `","`, which has seven thousand homes in a novel, or
gave up and marked the note a query for a human. So the failure was not that the
notes were hard; it was that the easy ones were being asked of the wrong thing.

This resolves them directly instead. Every rule is conservative in the same two
ways: the `find` it produces is always a **substring of the marked span**, so it
is grounded in text a reviewer actually pointed at rather than recalled; and any
note a rule cannot read with certainty returns None and goes to the model exactly
as before. A rule that is unsure is worth nothing, so it declines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from .apply import all_spans
from .model import (DESIGN, FORMAT_ITALIC, FORMAT_NO_SWASH, FORMAT_ROMAN,
                    FORMAT_SWASH, JUDGMENT, MECHANICAL, PARA_DELETE,
                    PARA_MERGE_NEXT, PARA_SPLIT_AT)
from .overgrab import repair_from_note, repair_pair
from .textmatch import normalize

# How many words a marked span may hold and still be treated as naming its target.
# A highlight over a word or a short phrase is pointing at it; a sticky note's
# anchor is the whole line it sits on, which points at nothing in particular, and
# a rule that guessed a target inside one would be doing the thing this module
# exists to avoid.
MAX_ANCHOR_WORDS = 12
# The same for the note: a short note is a label, a long one is prose to read.
MAX_NOTE_WORDS = 8

# Punctuation a reviewer names in words, and the character the house sets. The
# curly quotes and the true dashes are deliberate — the book is normalized to
# them, so a rule that inserted a straight quote would introduce the very
# inconsistency the review pass exists to catch.
PUNCTUATION = {
    "comma": ",",
    "period": ".",
    "full stop": ".",
    "question mark": "?",
    "exclamation point": "!",
    "exclamation mark": "!",
    "ellipsis": "…",
    "em dash": "—",
    "en dash": "–",
    "hyphen": "-",
    "colon": ":",
    "semicolon": ";",
    "apostrophe": "’",
    "single quote": "’",
    "single quotes": "’",
    "double quote": "”",
    "double quotes": "”",
}
# Openers, for the marks that come in pairs.
_OPENERS = {"’": "‘", "”": "“"}
# Words that make a note an instruction to carry out rather than a label to
# substitute. "did" against "done" is a replacement; "Delete" is not.
_VERBS = (
    "replace", "remove", "delete", "add", "insert", "capitalize", "capitalise",
    "lowercase", "lower case", "cap ", "italic", "de-italic", "roman",
    "hyphenate", "close up", "spell out", "enclose", "possessive", "should",
    "check", "confirm", "unsure", "confusing", "recommend", "designer",
    "previously", "query", "au:", "comp:", "stet", "move", "delete",
)
# Sentence punctuation trimmed off a marked span to find the word inside it. The
# quotes are NOT here: a mark on ‘Sides is pointing at the quote as much as the
# word, and the rules that care handle it themselves.
_EDGE = " \t\n,.;:!?"


@dataclass(frozen=True)
class Resolved:
    """A note read into an exact edit over the marked span."""
    find: str
    replace: str
    kind: str = MECHANICAL
    rule: str = ""                     # which rule fired, for the report
    format: str = ""                   # character formatting, instead of a rewrite
    paragraph: str = ""                # a whole-paragraph operation
    paragraph_style: str = ""          # the paragraph style to apply


def resolve(instruction: str, anchor: str, *, context: str = "",
            highlighted: bool = True) -> Resolved | None:
    """The edit a note makes to the span it is attached to, or None.

    `anchor` is the marked text — a highlight's own span, which is what makes a
    rule safe to run. `highlighted` is False for a sticky note, whose anchor is
    only the line it sits on: such a note names no target, so every rule that
    needs one declines.

    `context` is the whole line the mark sat on. It is used only by the one rule
    whose note reaches past the marked words by construction — "replace the comma
    with a period and capitalize *she*", where "she" is the next word and outside
    the highlight. Every other rule stays inside the mark.

    None means "no rule is certain about this", and the caller should fall back to
    the model. That is the common case for anything genuinely discursive, and it is
    the only safe answer for anything else."""
    note = (instruction or "").strip()
    anchor = (anchor or "").strip()
    context = (context or "").strip()
    if not note or not anchor:
        return None
    low = note.lower()

    # A long mark points at nothing in particular, so the rules that have to pick a
    # target inside it decline. `_line_break_op` is the exception, and only because
    # it never picks: the note quotes the words either side of the break, and the
    # mark is just the book text those are cut from. A note about a break has to
    # carry a whole sentence to be about anything, so applying the guard to it
    # would be refusing every one of them.
    rules = (_paragraph_op, _line_break_op, _discretionary_hyphen,
             _italic, _swash, _literal_after_colon, _replace_punctuation,
             _remove_punctuation, _add_punctuation, _case_change,
             _named_capital, _hyphenate, _enclose_in_quotes, _quote_style,
             _quoted_proposal, _bare_replacement, _composition_check)
    if len(anchor.split()) > MAX_ANCHOR_WORDS:
        # `_line_break_op` never picks a target — the note quotes the words either
        # side of the break — and `_replace_punctuation_named` is held to the notes
        # that name their own mark, so neither is guessing inside a long span. A
        # reviewer who highlights a whole line and writes "replace hyphen with en
        # dash" has still said exactly which hyphen when only one of them stands
        # between two digits.
        rules = (_line_break_op, _replace_punctuation_named)

    for rule in rules:
        got = rule(low, note, anchor, highlighted)
        if got is None and rule is _replace_punctuation and context:
            got = _replace_punctuation_over_line(low, note, anchor, context)
        if got is not None and got.find and got.find in (anchor + "\n" + context):
            got = _house_apostrophe(got)
            if (got.find == got.replace and not got.format and not got.paragraph
                    and not got.paragraph_style and got.kind != DESIGN):
                return None            # a rule that changes nothing is no rule
            return got
    return None


# A straight apostrophe inside a word, which is what a reviewer types into a
# comment box and never what the book contains.
_STRAIGHT_APOSTROPHE = re.compile(r"(?<=\w)'(?=\w)")


def _house_apostrophe(got: "Resolved") -> "Resolved":
    """The same edit with its replacement set in the book's own apostrophe.

    Half these rules take their answer from the reviewer's note verbatim — that is
    the whole point of them, and it is right. But a note is typed into a comment
    box, where "didn't" gets a straight quote, and the book is set in curly ones.
    Applying it as typed corrects the word and introduces a typographic
    inconsistency in the same stroke, which is precisely what the review pass
    downstream exists to catch. Twenty-seven of them on one proof.

    Two shapes are converted, both unambiguous: one *inside* a word ("didn't"),
    and a single one *ending* a word when it is the only straight mark left
    ("callin'", "Shanklins'") — an elided g or a plural possessive, which cannot be
    the closing half of a pair because there is no opening half. A leading one
    ("'til") is left alone: an elision and an opening quotation look identical
    there, and guessing the direction is not a thing to do silently."""
    if "'" not in got.replace:
        return got
    fixed = _STRAIGHT_APOSTROPHE.sub("’", got.replace)
    if fixed.count("'") == 1:
        at = fixed.index("'")
        if at > 0 and (fixed[at - 1].isalnum() or fixed[at - 1] == "’"):
            fixed = fixed[:at] + "’" + fixed[at + 1:]
    if fixed == got.replace:
        return got
    return replace(got, replace=fixed)


# A period or comma sitting anywhere but the front of a run of closing quotation
# marks. US practice — Chicago, and this book — puts both inside every quotation
# they end, and every other mark outside, so `pot’.”` and `appétit”,` are wrong in
# the same way and `pot.’”` is the answer to both. Held to a run carrying the
# double mark on purpose: a lone ’ is as likely to be a plural possessive as a
# closing quote ("the Shanklins’,"), and moving a comma through one of those
# would break a word to fix nothing.
_QUOTE_PUNCT = re.compile(r"([’”]+)([,.])([’”]*)")


def _tuck(m: "re.Match") -> str:
    """One run of closing quotation marks with its comma or period moved to the
    front of the run — inside every quotation the run closes."""
    return m.group(2) + m.group(1) + m.group(3)


def _tuck_quotes(text: str) -> str:
    """`text` with each such run put right, leaving the runs that carry no double
    mark alone."""
    return _QUOTE_PUNCT.sub(
        lambda m: _tuck(m) if "”" in m.group(1) + m.group(3) else m.group(0),
        text)


# A note that asks for something to be done, matched on stems so a participle
# ("enclosed", "italicized") reads the same as the imperative.
_ASKS = re.compile(r"\b(?:replac|remov|delet|add|insert|capitali[sz]|lowercas"
                   r"|italici[sz]|roman|hyphenat|enclos|swap|chang|correct"
                   r"|spell out|close up|should be|should have|needs? to be)",
                   re.IGNORECASE)
# …and the notes that ask for the opposite. "Stet" is an instruction to leave the
# text exactly as it is, so a no-op is the whole of what it wanted.
_LEAVE_IT = re.compile(r"\bstet\b|\bleave (?:it|as|this)\b|\bno change\b"
                       r"|\bas set\b", re.IGNORECASE)


def asks_for_a_change(note: str) -> bool:
    """Whether a reviewer's note is asking for the text to be different.

    Used to tell a real no-op from a mark that was never answered: a note reading
    "already fine" is satisfied by changing nothing, and one reading "song title
    should be enclosed in quotes, not italicized" is not."""
    note = note or ""
    return bool(_ASKS.search(note)) and not _LEAVE_IT.search(note)


def house_typography(edits):
    """`edits` with the two typographic slips a replacement picks up on its way out
    of a comment box put right.

    A reviewer's note is typed, not typeset. It carries the straight apostrophe the
    comment box gives them, and it is written from memory of a sentence rather than
    against the character stream — so a replacement can arrive with "I'd" in it, or
    with the period parked outside the quotation mark it belongs inside. Applied
    verbatim, each corrects one thing and introduces another, and the second one is
    invisible to everything downstream: `verify` confirms the file matches the edit,
    because it does.

    Both fixes touch the replacement only, and only where the edit is what
    introduced the shape — a `find` that already carries it is book text the
    reviewer did not mark, and rewriting that here would be this engine making a
    change nobody asked for. The rules already do this to their own answers
    (`_house_apostrophe`); this is the same thing for every edit, whatever read it.
    """
    out = []
    for e in edits:
        fixed = e.replace
        if "'" in fixed and "'" not in e.find:
            fixed = _house_apostrophe(Resolved(e.find, fixed)).replace
        tucked = _tuck_quotes(fixed)
        if tucked != fixed and _tuck_quotes(e.find) == e.find:
            fixed = tucked
        out.append(e if fixed == e.replace else replace(e, replace=fixed))
    return out


def enforce_note_fidelity(edits):
    """`edits` with each one pulled back to the plain sense of its note — a
    "remove" that a pass turned into a substitution, a written-out literal whose
    case was dropped, a spell-out that lost its qualifier (see
    `overgrab.repair_from_note`).

    The extraction repair does this to the model's first draft; this is the same
    repair over the finished list, so a slip a *later* pass introduced is caught
    too — a re-anchored "Remove comma" that came back as a comma-to-period swap
    was the one that shipped a broken line last time. Only concrete text edits are
    touched: a format, paragraph or design edit names no pair to repair, and the
    guards inside each repair decline anything that is not its exact shape."""
    out = []
    for e in edits:
        if e.is_format or e.is_layout or e.kind == DESIGN or not e.find:
            out.append(e)
            continue
        find, rep = repair_from_note(e.find, e.replace, e.instruction)
        out.append(e if (find, rep) == (e.find, e.replace)
                   else replace(e, find=find, replace=rep))
    return out


def _replace_punctuation_over_line(low, note, anchor, context) -> Resolved | None:
    """`Replace comma with period and capitalize "she"`, worked over the whole line
    the mark sat on.

    A reviewer highlights the comma, not the word after it, so this one note
    reaches outside its own mark by construction — and declining it costs the
    commonest instruction on a proof after the bare word swap. Widening is safe
    because the note says exactly which mark it means: the one the named word
    follows. A line with two commas in it is not ambiguous when only one of them
    has "besides" on its right."""
    want = _capitalize_word(low)
    if not want:
        return None
    swap = re.match(r"^(?:replace|change)\s+(?:the\s+)?([a-z ]+?)\s+with\s+"
                    r"(?:an?\s+|the\s+)?([a-z ]+?)\s*$",
                    _note_core(_strip_capitalize(low)))
    if not swap:
        return None
    old = PUNCTUATION.get(swap.group(1).strip())
    new = PUNCTUATION.get(swap.group(2).strip())
    if old is None or new is None:
        return None
    at = _mark_position(context, old, new, want)
    if at is None:
        return None
    replaced = _swap_mark(context, at, new, want)
    if replaced is None:
        return None
    return _focus(context, replaced, "replace-punctuation-over-line")


# --- adjudicating an author's reply to a proofreader's mark --------------------
# A proof often makes a second pass through the author, who answers the
# proofreader's queries in place: a reply annotation hanging off the mark. That
# reply is the author's decision on the mark, and there are three of them —
#   accept:  "correct", "yes", "please change" — carry out the marked change;
#   dismiss: "no", "stet", "leave as is"        — the text stays as it is;
#   swap:    "change to 'leaves'", "remove"     — the author's own correction,
#                                                 which wins over the mark.
# Only `dismiss` is acted on deterministically, and only when the whole reply is
# unmistakably a rejection: it settles the flag with no change and no model call.
# accept and swap need the mark and the reply read together — which copy, in the
# book's own spelling, with the author's word substituted — so they go to the
# model, which is shown the reply and told the author's decision is final. The
# verdict is still returned for all three, so the change log can say a dismissed
# mark was declined by the author rather than lost.

REPLY_ACCEPT = "accept"
REPLY_DISMISS = "dismiss"
REPLY_SWAP = "swap"
REPLY_DEFER = "defer"           # unclear or mixed — the model decides

# A reply is *only* one of these when, stripped of trailing punctuation, that is
# the whole of it. "No" is a dismissal; "No, change it to 'leaves'" is not — it
# carries a correction, so it is a swap the model reads. The phrases are matched
# whole (not as substrings) for exactly that reason.
_REPLY_REJECT = frozenset([
    "no", "nope", "stet", "leave", "leave it", "leave as is", "leave as-is",
    "leave alone", "leave it as is", "leave as it is", "as is", "as-is",
    "keep", "keep it", "keep as is", "keep as-is", "keep it as is",
    "keep original", "keep the original", "unchanged", "no change",
    "no changes", "don't change", "do not change", "dont change", "ignore",
    "disregard", "n/a", "na", "never mind", "nevermind", "skip", "no thanks",
    "fine as is", "fine as-is", "it's fine", "its fine", "ok as is",
])
_REPLY_AFFIRM = frozenset([
    "correct", "correct.", "yes", "yep", "yeah", "y", "ok", "okay", "agreed",
    "agree", "approved", "approve", "accept", "accepted", "confirmed",
    "confirm", "right", "good", "yes please", "please change", "change",
    "change it", "change this", "please change it", "make the change",
    "do it", "fix", "fix it", "please fix", "yes change", "yes, change",
    "please change, correct", "change, correct", "correct, please change",
])
# Wording that means the author is giving their own correction rather than a
# yes/no: an instruction verb, a "change to X" / "should be X", or a quoted run.
_REPLY_SWAP_HINT = re.compile(
    r"\b(change\s+to|changed\s+to|replace\s+with|should\s+(?:be|read)|"
    r"instead|use\b|make\s+it|remove|delete|insert|add\b|reword|rephrase|"
    r"lowercase|capitali[sz]e|italici[sz]e)\b", re.IGNORECASE)
_REPLY_QUOTE = re.compile(r"[“\"'‘][^”\"'’]{1,}[”\"'’]")


def _reply_norm(reply: str) -> str:
    """A reply reduced for whole-phrase matching: lowercased, its outer quotes
    and trailing sentence punctuation stripped, inner spaces collapsed."""
    r = " ".join((reply or "").split()).strip().strip("\"'“”‘’")
    return r.rstrip(".!").strip().lower()


def adjudicate_reply(note: str, replies: tuple[str, ...]) -> str:
    """The author's decision on a proofreader's mark, read off their reply.

    Returns `REPLY_ACCEPT`, `REPLY_DISMISS`, `REPLY_SWAP`, or `REPLY_DEFER`. Only
    the *last* reply is decisive — a thread ends on the author's final word — but
    an earlier reply's wording still counts as the correction when the last is a
    bare confirmation ("yes" under a "change to 'leaves'"). Conservative on both
    sides: a reply that is not unmistakably one of the three is deferred to the
    model, because guessing wrong here either applies a change the author refused
    or drops one they asked for."""
    texts = [r for r in replies if (r or "").strip()]
    if not texts:
        return REPLY_DEFER
    last = _reply_norm(texts[-1])
    # A swap anywhere in the thread carries its own wording, and that wording is
    # the point of the reply even if the author then adds a bare "yes".
    for r in texts:
        if _REPLY_SWAP_HINT.search(r) or _REPLY_QUOTE.search(r):
            # ...unless it is only affirming the mark's own quoted answer — but a
            # verb like "remove"/"change to" is the author writing the edit, so
            # the swap reading stands whenever the hint is a verb or a new quote.
            return REPLY_SWAP
    if last in _REPLY_REJECT:
        return REPLY_DISMISS
    if last in _REPLY_AFFIRM:
        return REPLY_ACCEPT
    # Two very common compound confirmations that are not in the set verbatim.
    if last.startswith(("yes", "correct", "please change", "approved", "confirmed")):
        return REPLY_ACCEPT
    if last.startswith(("no", "leave", "keep", "stet", "ignore", "disregard")):
        return REPLY_DISMISS
    return REPLY_DEFER


def edits_from_comments(comments, pages=None,
                        pdf_pages=None) -> tuple[list[dict], list]:
    """Split a proof's comments into the ones the rules can resolve and the ones
    the model still has to read.

    Returns `(rows, unresolved)`. Each row is a corrections entry in the same shape
    the extractor emits — `find`/`replace`/`context`/`instruction`/`kind`/`source` —
    so the two halves merge into one list a reviewer reads before anything is
    applied, and neither half can tell which produced which. `unresolved` is the
    comments to batch to the model, and it is the only thing that costs anything.

    The page is not carried here: an edit cites its comment's id in `source`, and
    that id already names the page (`parse.page_from_source`), so it arrives on the
    edit without anyone having to retype it.

    `pages` maps a proof page to THE BOOK'S OWN TEXT for that page (what
    `pagemap.page_book_text` returns), and it settles what two marks on the same
    words mean. The book's text and not the proof's for the *count*: an ordinal is
    read back after `apply` has narrowed to the page's run of the book, so the
    number of copies it indexes has to be the number there — a running head and a
    word hyphenated across a line end are copies the book does not have. But the
    mark's own offset was measured against the PDF's rendering, so `pdf_pages` — the
    proof's page texts — is where that offset is exact and where the copy is
    actually located; the two are reconciled in `_ordinal`, which trusts the offset
    only when both renderings agree on how many copies the page holds. A reviewer
    marking a quotation puts a note on the opening mark and another on the closing
    one — one request, recorded twice — while a reviewer marking both copies of
    "‘Baba’" in a paragraph means two. Those look identical in the edit list and
    are told apart by where the marks sit: the same position is one request, and
    different positions are different copies of the text, whose ordinal on the page
    is what lets both land."""
    rows: list[dict] = []
    unresolved: list = []
    made: list[tuple] = []                 # (comment, Resolved) in reading order
    for c in comments:
        if getattr(c, "replies", ()):
            # An author's reply is a decision on the proofreader's mark, and the
            # author's to make (`adjudicate_reply`). A plain rejection ("no",
            # "stet", "leave as is") is settled here and for free: the text stays
            # as it is, so no edit is made and no model call is spent turning a
            # "leave it" into a change. Everything else — a confirmation, or the
            # author's own wording in place of the mark — goes to the model, which
            # is shown the reply beside the note and told the author's word wins.
            # (`run` reads the same verdict to record a dismissed mark as resolved
            # rather than as a mark nobody acted on.)
            if adjudicate_reply(getattr(c, "instruction", "") or "",
                                tuple(c.replies)) == REPLY_DISMISS:
                continue
            unresolved.append(c)
            continue
        instruction = getattr(c, "instruction", "") or ""
        anchor = getattr(c, "anchor", "") or ""
        context = getattr(c, "context", "") or ""
        highlighted = getattr(c, "kind", "") == "highlight"
        got = resolve(instruction, anchor, context=context,
                      highlighted=highlighted)
        if got is None:
            unresolved.append(c)
        else:
            made.append((c, got))

    seen: dict[tuple, dict] = {}
    for c, got in made:
        instruction = getattr(c, "instruction", "") or ""
        anchor = getattr(c, "anchor", "") or ""
        context = getattr(c, "context", "") or ""
        page = getattr(c, "page", 0) or 0
        offset = getattr(c, "offset", -1)
        key = (page, offset, instruction, got.find, got.replace)
        if key in seen:
            # The same mark, noted twice. One edit, and both comments cite it, so
            # the change log still accounts for each of them and neither is left
            # looking like a mark nobody acted on.
            cid = getattr(c, "id", "")
            if cid:
                seen[key]["source"] = f"{seen[key].get('source', '')} {cid}".strip()
            continue
        row = {"find": got.find, "replace": got.replace,
               "instruction": instruction}
        if got.format:
            row["format"] = got.format
        if got.paragraph:
            row["paragraph"] = got.paragraph
        if got.paragraph_style:
            row["paragraph_style"] = got.paragraph_style
        if got.kind != MECHANICAL:
            row["kind"] = got.kind
        if context and context != got.find:
            row["context"] = context
        if getattr(c, "id", ""):
            row["source"] = c.id
        nth = _ordinal(pages, page, offset, anchor, got.find, pdf_pages=pdf_pages)
        if nth:
            row["occurrence"] = nth
        seen[key] = row
        rows.append(row)
    return rows, unresolved


def _page_text(pages, page: int) -> str:
    """The text of `page` from `pages` — a dict keyed by page, or a list indexed
    from page 1 — or "" when the page is not there."""
    if not pages:
        return ""
    if hasattr(pages, "get"):
        return pages.get(page) or ""
    return pages[page - 1] if 1 <= page <= len(pages) else ""


def _ordinal(pages, page: int, offset: int, anchor: str, find: str,
             pdf_pages=None) -> int:
    """Which copy of `find` on the proof page this mark sits on, 1-based — or 0
    when the question does not arise or cannot be answered.

    0 for text that occurs once on the page, which is the ordinary case: an edit
    with no ordinal is the one that insists its anchor be unique, and that is a
    stronger check than any number. An ordinal is only worth carrying when the page
    holds several copies and the mark says which was meant.

    The count is taken over the *page* — what the reviewer was looking at — so it
    is only ever read after the page map has narrowed to that page; `apply` refuses
    an ordinal it cannot scope that way. And the count that has to hold is the
    book's: `apply` narrows to the book's text for the page, so the Nth copy the
    ordinal names must be the Nth copy *there*.

    But the mark's `offset` is measured in the PDF page's own text — running head
    and folio included — a different coordinate system from the book story. So the
    copy is located in `pdf_pages`, where the offset is exact, and the ordinal is
    trusted only when the two renderings hold the same number of copies: that
    equality is what proves the running head added none, so a rank counted in the
    one is a rank in the other. Dropping the PDF offset straight into the book text
    was the bug this replaces — a running head's characters shifted every offset
    past the first, so the mark fell between the book's copies and chose none of
    them. With no `pdf_pages` the offset is read as the page text's own coordinate,
    the contract the callers without a rendering still rely on."""
    if not pages or offset < 0 or not find:
        return 0
    book = _page_text(pages, page)
    if not book:
        return 0
    book_spans = all_spans(book, find)
    if len(book_spans) <= 1:
        return 0
    src = book
    if pdf_pages is not None:
        rendered = _page_text(pdf_pages, page)
        if rendered:
            src = rendered
    src_spans = book_spans if src is book else all_spans(src, find)
    if len(src_spans) != len(book_spans):
        # The renderings disagree on how many copies the page holds — a running
        # head, or a word hyphenated across a line, is a copy one has and the other
        # does not — so a rank in the PDF is not a rank in the book. Refuse rather
        # than miscount; the anchor's own uniqueness check still guards `apply`.
        return 0
    at = offset + (anchor.find(find) if find in anchor else 0)
    for i, (start, end) in enumerate(src_spans, 1):
        if start <= at < end or abs(start - at) <= 2:
            return i
    return 0


def _cget(comment, field: str, default=""):
    """A field off a reviewer comment, whether it is the app's `PdfComment`
    dataclass or the plain dict a job carries it as once it has been through
    JSON."""
    if isinstance(comment, dict):
        return comment.get(field, default)
    return getattr(comment, field, default)


def widen_edits_to_marks(edits, comments, *, book_pages=None):
    """`edits` with a `find` that quoted less than the reviewer highlighted put
    back to the whole highlighted run.

    A highlight with a bare word written beside it is the oldest mark there is:
    it says "this run becomes that". "is hoping" highlighted, "hoped" written —
    both words go, and one word arrives. A model shown the note alone tends to
    match the parts of speech instead and emit "hoping" → "hoped", which applies
    perfectly and leaves "he is hoped" in the book. `verify` then confirms the
    file matches the edit, because it does; nothing downstream can see it. The
    mark can, and the mark is right here.

    Held to the case where the reading is not in doubt:

      * the mark is a highlight, and the note is *exactly* the replacement — a
        substitution written out, not an instruction ("Lowercase") to carry out;
      * the highlighted run contains the find and adds no more than one word to
        it, so a note attached to a whole line ("three" against "…and a 3-mile")
        can never swallow the line;
      * the extra is a word, not punctuation — the marks around a word are
        `overgrab`'s business, and widening onto them would undo it;
      * and the widened find is text the page really carries, so an anchor made
        from the PDF's rendering of a highlight can never lose an edit that was
        landing before.
    """
    if not edits or not comments:
        return edits
    by_id = {}
    for c in comments:
        cid = _cget(c, "id")
        if cid:
            by_id.setdefault(cid, c)
    out = []
    for e in edits:
        c = by_id.get((e.source or "").split()[0]) if e.source else None
        wider = _mark_run(e, c, book_pages) if c is not None else ""
        out.append(replace(e, find=wider) if wider else e)
    return out


def _mark_run(edit, comment, book_pages) -> str:
    """The highlighted run an edit should have quoted, or "" when this edit is
    not that case. See `widen_edits_to_marks` for each condition."""
    if edit.format or edit.paragraph or edit.kind == DESIGN or not edit.find:
        return ""
    if _cget(comment, "kind") != "highlight":
        return ""
    anchor = (_cget(comment, "anchor") or "").strip()
    note = (_cget(comment, "instruction") or "").strip()
    if not anchor or not note or note != edit.replace.strip():
        return ""
    marked, quoted = (normalize(anchor, fold_case=True),
                      normalize(edit.find, fold_case=True))
    if not quoted or quoted == marked or quoted not in marked:
        return ""
    # What the highlight has that the find does not. A find the anchor carries
    # only once it is normalized leaves the whole anchor here, which is more than
    # a word and declines below — the conservative answer either way.
    extra = anchor.replace(edit.find, " ", 1) if edit.find in anchor else anchor
    if not any(ch.isalnum() for ch in extra):
        return ""                          # only punctuation to spare
    if len(extra.split()) > 1:
        return ""                          # a line, not the word beside the find
    wider, _ = repair_pair(anchor, edit.replace, edit.instruction)
    book = _page_text(book_pages, edit.page)
    if not book or not all_spans(book, wider):
        return ""                          # the book does not carry it
    return wider


def fill_edit_occurrences(edits, comments, *, book_pages=None, pdf_pages=None):
    """`edits` with each one's `occurrence` set from the mark it was read from.

    The rule-resolved edits already carry the ordinal `edits_from_comments`
    computed; a model-read edit does not — the extraction schema asks the model for
    it, and a page dumped to text is exactly where a model miscounts, so a repeated
    word arrives with no ordinal and `apply` can only flag it. This fills it the one
    deterministic way there is, and the same way the rule path does: the citing
    comment's own `offset` says which copy the reviewer marked. A find unique on its
    page keeps occurrence 0 — the uniqueness check is stronger than any number, and
    this clears a stray one a model may have invented; a find that repeats and whose
    mark locates gets that copy's ordinal; a find that repeats but cannot be located
    (no offset, an unplaced page, renderings that disagree on the count) is left
    exactly as extracted, guessed at by nothing."""
    if not edits or not comments:
        return edits
    by_id: dict = {}
    for c in comments:
        cid = _cget(c, "id")
        if cid:
            by_id.setdefault(cid, c)
    out = []
    for e in edits:
        occ = e.occurrence
        c = by_id.get((e.source or "").split()[0]) if e.source else None
        if c is not None and e.find:
            page = e.page or (_cget(c, "page", 0) or 0)
            book = _page_text(book_pages, page)
            copies = all_spans(book, e.find) if book else []
            if copies:
                if len(copies) <= 1:
                    occ = 0            # unique: the uniqueness check, not a number
                else:
                    nth = _ordinal(book_pages, page, _cget(c, "offset", -1),
                                   _cget(c, "anchor", "") or "", e.find,
                                   pdf_pages=pdf_pages)
                    if nth:
                        occ = nth
        out.append(e if occ == e.occurrence else replace(e, occurrence=occ))
    return out


# --- the rules ----------------------------------------------------------------

# Notes about how the page *composed* — where a line broke, whether a heading was
# left stranded, whether the rag reads badly. Each is a result of InDesign setting
# the text, so nothing this engine can compare will settle it. They are kept as
# located checks for a person rather than turned into an edit or, as before, dropped
# into "a design request" with no page and nothing to act on.
# Matched on word boundaries, which is not fussiness: a bare "rag" is a substring of
# "paragraph", and every note about a paragraph would have been swallowed as a note
# about the rag.
_COMPOSITION = re.compile(
    r"\b(?:bad (?:line )?breaks?|breaks? (?:here|badly)|widows?|orphans?"
    r"|loose lines?|tight lines?|(?:loose|bad) rags?|rags?"
    r"|runs? (?:long|short)|short pages?|stacks?|ladders?|rivers?"
    r"|line spacing|leading|too (?:much|little) space|tracking|kerning"
    r"|hyphenation|bad hyphens?|reflow|recompose|designer)\b", re.IGNORECASE)

# Alignment and indentation are set on the paragraph in InDesign, not written into
# the story — "flush left", "align right", "centre this", "no indent", "ragged
# right". There is nothing in the text this engine can change to satisfy them and
# nothing a file comparison could confirm, exactly as with _COMPOSITION above. Left
# to the model, such a note is read as prose ("set X flush left as a new paragraph")
# and mis-applied as a forced paragraph break; kept here it becomes a located check
# the designer carries out in InDesign. Scoped to unambiguous typesetting phrasings
# so a genuine text edit is never swallowed as a design note.
_TYPESETTING = re.compile(
    r"\bflush[\s-]*(?:left|right)\b"
    r"|\b(?:align|aligned|alignment|set|range|ranged)\b[^.]{0,20}?"
      r"\b(?:left|right|centre|center|flush|justified?)\b"
    r"|\b(?:left|right|centre|center|fully)[\s-]?align(?:ed|ment)?\b"
    r"|\bjustif(?:y|ied|ication)\b|\bunjustified\b"
    r"|\bragged?\s+(?:right|left)\b"
    r"|\bcent(?:re|er)\s+(?:this|the|it)\b"
    r"|\b(?:no|remove|delete|suppress|without)\s+(?:the\s+)?"
      r"(?:first[\s-]?line\s+)?indent(?:ation)?\b"
    r"|\b(?:first[\s-]?line|hanging|full)\s+indent(?:ation)?\b"
    r"|\bindent\s+(?:this|the|it)\b", re.IGNORECASE)

# Whole-paragraph requests a reviewer states in prose, and the operation each means.
# Ordered longest first so "start on a new page" is not read as "new page" applied
# to something else.
_PARA_PHRASES = (
    ("recto", r"\brecto\b|\bright-?hand page\b"),
    ("verso", r"\bverso\b|\bleft-?hand page\b"),
    ("page-break", r"start(?:s)?\b[^.]*\bon a new page|\bpage break before\b"
                   r"|\bbreak to a new page\b|\bnew page here\b"),
    ("column-break", r"\bcolumn break\b|start(?:s)?\b[^.]*\ba new column\b"),
    ("keep-with-next", r"\bkeep\b[^.]*\bwith (?:the )?"
                       r"(?:next|text|following|para|line)\b"
                       r"|(?:do not|don'?t) strand\b"),
    ("keep-together", r"\bkeep\b[^.]*\btogether\b"
                      r"|(?:do not|don'?t) break this paragraph\b"),
    ("allow-break", r"\bmay break\b|\ballow this to break\b"),
    (PARA_DELETE, r"(?:delete|remove|cut) this (?:paragraph|line)\b"),
)
_PARA_PATTERNS = tuple((op, re.compile(pat, re.IGNORECASE))
                      for op, pat in _PARA_PHRASES)


def _composition_check(low, note, anchor, highlighted) -> Resolved | None:
    """A note about how the page came out, kept as a check rather than an edit.

    Whether a break reads well or a heading is stranded is a fact about the composed
    page, and composing is InDesign's job — so there is nothing here to apply and
    nothing a file comparison could confirm. What this engine *can* do is say where
    the note was: the edit anchors, changes nothing, and comes back as a located row
    on the list a designer works through. That is strictly more than the "a design
    request, not a text edit" it used to become, which carried no page at all.

    Runs last, so a note that *is* appliable — "hyphenate after Lime", "start this
    chapter on a recto" — is applied by the rule that can, and only what is left
    becomes a check."""
    if not (_COMPOSITION.search(note) or _TYPESETTING.search(note)):
        return None
    find = anchor.strip(_EDGE)
    if not find:
        return None
    return Resolved(find, find, DESIGN, "composition-check")


def _paragraph_op(low, note, anchor, highlighted) -> Resolved | None:
    """`Start this chapter on a recto`, `keep this heading with the next paragraph`.

    These read as layout and are not: where a paragraph starts and whether it may be
    split are properties of the paragraph, carried in the story next to its text. The
    marked words say which paragraph; the operation is applied to the whole of it."""
    # "Should we cut this paragraph?" names the operation and asks for it at the
    # same time. A question is a query for a person, and reading one as an
    # instruction is worst here of all, where the instruction deletes copy.
    if note.rstrip().endswith("?"):
        return None
    op = next((name for name, pattern in _PARA_PATTERNS
               if pattern.search(note)), None)
    if op is None:
        return None
    find = anchor.strip(_EDGE)
    if not find:
        return None
    return Resolved(find, find, MECHANICAL, f"paragraph-{op}", paragraph=op)


def _line_break_op(low, note, anchor, highlighted) -> Resolved | None:
    """`Delete the line break between "stuff:" and "Rotting"`, `insert a paragraph
    break before "The next hour"`.

    The commonest note on a proof of verse, and one this engine used to refuse on
    principle: the reviewer's sentence ran across a break, so the anchor did too.
    But which break they mean is stated exactly — they quote the words on either
    side of it — so there is nothing to interpret. The find is cut from the marked
    text itself rather than assembled from the note's quotes, which keeps it
    verbatim book text and lets the caller's "must be inside the mark" check do its
    job.

    Declines on a question, as `_paragraph_op` does: "should this run on?" is a
    query for a person, not an instruction."""
    if note.rstrip().endswith("?"):
        return None
    source = anchor
    quoted = re.findall(r"[\"“‘']([^\"“”‘’']{2,60})[\"”’']", note)
    if re.search(r"\b(?:delete|remove|close up|take out)\b[^.]*?\b"
                 r"(?:line|paragraph)\s+breaks?\b", low):
        # Two quoted sides name the break; one names the word the break sits before.
        span = _span_between(source, quoted)
        if span is None:
            # "delete the line break before X" names one side. The break is the one
            # in front of that word, so the anchor reaches back over the few words
            # before it — enough to cross the break and no more.
            span = _span_before(source, quoted)
        if span is None:
            return None
        return Resolved(span, span, MECHANICAL, "merge-next",
                        paragraph=PARA_MERGE_NEXT)
    if re.search(r"\b(?:insert|add|put)\b[^.]*?\b(?:paragraph|line)\s+breaks?\b"
                 r"|\bstart a new paragraph\b|\bbreak (?:this )?into "
                 r"(?:two|separate) paragraphs\b", low):
        for q in quoted:
            at = source.find(q)
            if at > 0:
                return Resolved(source[at:at + len(q)], source[at:at + len(q)],
                                MECHANICAL, "split-at", paragraph=PARA_SPLIT_AT)
        return None
    return None


def _span_between(text: str, quoted: list[str]) -> str | None:
    """The run of `text` from the first quoted word to the end of the second, when
    both are there and in that order — the words either side of the break the note
    names, cut out of the book's own text so the anchor stays verbatim."""
    if len(quoted) < 2:
        return None
    first, second = quoted[0], quoted[1]
    a = text.find(first)
    if a == -1:
        return None
    b = text.find(second, a + len(first))
    if b == -1:
        return None
    return text[a:b + len(second)]


# How many words in front of a named word the anchor reaches back over, when the
# note gives only the far side of the break ("delete the line break before dead").
# Enough to be sure of crossing the break, few enough that the run stays inside the
# sentence the reviewer marked.
_BREAK_LOOKBACK = 4


def _span_before(text: str, quoted: list[str]) -> str | None:
    """The run of `text` ending at the first quoted word and reaching back over the
    words in front of it — the anchor for a break named only by what follows it."""
    if not quoted:
        return None
    word = quoted[0]
    at = text.find(word)
    if at <= 0:
        return None
    before = text[:at].split()
    if not before:
        return None
    lead = " ".join(before[-_BREAK_LOOKBACK:])
    start = text.find(lead)
    if start == -1:
        return None
    return text[start:at + len(word)]


def _swash(low, note, anchor, highlighted) -> Resolved | None:
    """`Swoop the R`, `swash the S so it matches`, `remove the swash`.

    A flourished capital is not a layout request and not a different font: it is an
    alternate glyph the face already carries, switched on by an OpenType feature
    that an IDML writes on the character range. So this is the same shape as
    italics — the marked words are styled, the text is untouched.

    The whole marked word is styled, never the single letter the note names. A find
    of "R" is not an anchor (there are thousands), and it does not need to be: the
    feature substitutes only where the font has a swash form, so the rest of the
    word is left exactly as it was."""
    if not highlighted:
        return None
    if re.search(r"\b(?:no|remove|take off|kill|drop)\b[^.]*\bswash", low):
        want = FORMAT_NO_SWASH
    elif re.search(r"\bswash(?:e[sd])?\b|\bswoop(?:s|ed|ing)?\b"
                   r"|\bflourish(?:e[sd])?\b|\bcurl(?:s|ed)? the\b", low):
        want = FORMAT_SWASH
    else:
        return None
    # A note that quotes the word to style names its own target ("swoop the R in
    # \u201cAuthor\u201d"); otherwise the marked span is it.
    quoted = re.findall(r"[\"“‘']([^\"“”‘’']{2,60})[\"”’']", note)
    find = next((q for q in quoted if q in anchor), anchor.strip(_EDGE + "“”‘’\"'"))
    if not find or len(find.strip()) < 2:
        return None
    return Resolved(find, find, MECHANICAL, f"{want}-format", format=want)


def _discretionary_hyphen(low, note, anchor, highlighted) -> Resolved | None:
    """`Designer: bad break -- hyphenate after "Lime"`.

    The typesetter's own fix for a word broken in the wrong place is a discretionary
    hyphen: an invisible character that tells the composer where the word may divide.
    It is text, so this engine can put it in — and it is the one compositional note
    that does not have to wait for a person."""
    m = re.search(r'hyphenate\s+after\s+["“\']?([A-Za-z]{2,})["”\']?', note,
                  re.IGNORECASE)
    if not m:
        return None
    stem = m.group(1)
    word = next((w for w in re.findall(r"[\w’'-]+", anchor)
                 if w.lower().startswith(stem.lower()) and len(w) > len(stem)),
                None)
    if word is None:
        return None
    at = len(stem)
    return Resolved(word, word[:at] + "\u00ad" + word[at:], MECHANICAL,
                    "discretionary-hyphen")


def _italic(low, note, anchor, highlighted) -> Resolved | None:
    """`Italicize`, `Italicize movie title`, `De-italicize`.

    Not a layout request, whatever it looks like: the italics of a film or album
    title are part of the text as much as its spelling, and an IDML carries them on
    the character range around the words. So the mark names its own target — the
    highlighted span is exactly what gets styled — and the edit changes no text."""
    if not highlighted:
        return None
    if re.match(r"^(?:de-?italici[sz]e|roman|set\s+roman|un-?italici[sz]e)\b", low):
        want = FORMAT_ROMAN
    elif re.match(r"^italici[sz]e\b", low) or low in ("italic", "italics"):
        want = FORMAT_ITALIC
    else:
        return None
    # A note naming what to italicize ("Italicize movie title") is still about the
    # marked words; a note naming something else entirely is not this rule's.
    find = anchor.strip(_EDGE + "“”‘’\"'")
    if not find:
        return None
    return Resolved(find, find, MECHANICAL, f"{want}-format", format=want)

def _literal_after_colon(low, note, anchor, highlighted) -> Resolved | None:
    """`Add abbreviating apostrophe: callin'` — the note states the answer after a
    colon, so the tail is the replacement and the marked word is the find. The
    most reliable shape there is: a reviewer who writes the corrected text is not
    leaving anything to be worked out."""
    if ":" not in note:
        return None
    head, _, tail = note.partition(":")
    literal = tail.strip().rstrip(".").strip()
    if not literal or len(literal.split()) > MAX_NOTE_WORDS:
        return None
    if len(head.split()) > MAX_NOTE_WORDS:
        return None                    # prose that happens to hold a colon
    find = _target_of(anchor, literal)
    if find is None or literal == find:
        return None
    return Resolved(find, _kept_opener(low, find, literal), MECHANICAL,
                    "literal-after-colon")


def _kept_opener(low: str, find: str, literal: str) -> str:
    """The literal with the marked text's opening quotation mark put back, when
    the note asked for something to be *added*.

    "Add drop apostrophe: ’bout" against ‘bout is one character away from being
    the answer, and the character is not the reviewer's to lose: the ‘ opens a
    nested quotation and the ’ that closes it is further down the line. Writing
    the literal over the word converts the opener into the apostrophe and leaves
    the closer with no partner — `I said, ’bout damn time.’”`. A note that says
    *add* has said that nothing goes, so the mark keeps its opener and the
    apostrophe joins it."""
    if not re.match(r"^(?:add|insert)\b", low):
        return literal
    if find[:1] not in "‘“" or literal[:1] in "‘“":
        return literal
    return find[0] + literal


def _replace_punctuation_named(low, note, anchor, highlighted) -> Resolved | None:
    """`_replace_punctuation` with the "only one such mark in the span" evidence
    withdrawn — for a mark too long to be pointing at anything, where that count
    means nothing. What is left is the note naming its own target."""
    return _replace_punctuation(low, note, anchor, highlighted, strict=True)


def _replace_punctuation(low, note, anchor, highlighted, *, strict=False
                         ) -> Resolved | None:
    """`Replace comma with period`, and the same with a capitalized next word.

    Which mark is meant has to be settled by evidence, never by picking the first
    one — the whole failure this module was written for came from picking. Three
    things can settle it, in order of how strongly: the note names the word that
    follows the mark ("and capitalize \u201cbesides\u201d"), so the mark is the one
    that word comes after; the note asks for an en dash and exactly one hyphen in
    the span sits between digits, so the score is meant and not the hyphenated
    compound beside it; or the span holds the mark only once and there is nothing
    to choose. Anything else declines to the model."""
    m = re.match(r"^(?:replace|change)\s+(?:the\s+)?([a-z ]+?)\s+with\s+"
                 r"(?:an?\s+|the\s+)?([a-z ]+?)\s*$",
                 _note_core(_strip_capitalize(low)))
    if not m:
        # "Replace with em dash" — the mark to replace is whatever the span holds.
        m2 = re.match(r"^replace\s+with\s+(?:an?\s+|the\s+)?([a-z ]+?)\s*$",
                      _note_core(_strip_capitalize(low)))
        if not m2:
            return None
        target = PUNCTUATION.get(m2.group(1).strip())
        if target is None:
            return None
        hits = [c for c in anchor if c in ",.;:!?-–—…"]
        if target == "–" and _score_hyphen(anchor) is not None:
            # A range is the one thing an en dash is asked for, and the span it
            # sits in ordinarily holds other marks too — "We won 3-1, and I played
            # reasonably well" holds a comma. Requiring the span to carry a single
            # kind of mark sent every score to the model.
            old, new = "-", target
        elif strict or len(set(hits)) != 1:
            return None
        else:
            old, new = hits[0], target
    else:
        old = PUNCTUATION.get(m.group(1).strip())
        new = PUNCTUATION.get(m.group(2).strip())
        if old is None or new is None:
            return None
    word = _capitalize_word(low)
    at = _mark_position(anchor, old, new, word, strict=strict)
    if at is None:
        return None
    replaced = _swap_mark(anchor, at, new, word)
    if replaced is None:
        return None
    return _focus(anchor, replaced, "replace-punctuation")


# The dashes the house sets closed up, with no space on either side.
_CLOSED_UP = "—–"
# Whatever stands between a mark and the word a note names after it — a closing
# quotation mark, a space, or both, in any order. The space has to be inside the
# class, not a leading `\s*`: the commonest shape on a proof is a comma that closes
# a line of dialogue, `though,” she`, where a quotation mark AND a space sit between
# the comma and "she", and a pattern that allowed only quotes-then-word could not
# reach across the space to find her.
_QUOTES_THEN = r"[\s\"“”'‘’]*"

# The sentence-ending marks, and the shape of a dialogue tag right after one.
_TERMINALS = ".!?"
# A closing double quote, then whitespace, then the start of an attribution — the
# narrator's "I", another pronoun, or a capitalized name ("Isabella asked", "Jack
# boomed"). This is what marks the period that runs a line of dialogue into its
# tag, so "replace period with comma" can find it among a line's other periods.
_DIALOGUE_TAG_AFTER = re.compile(
    r'^["”]\s+(?:I\b|[Hh]e\b|[Ss]he\b|[Tt]hey\b|[Ww]e\b|[Yy]ou\b|[Ii]t\b'
    r'|[A-Z][a-z]+)')


def _mark_position(text: str, old: str, new: str, word: str = "", *,
                   strict: bool = False) -> int | None:
    """The offset in `text` of the mark a note means, or None when nothing in the
    note or the text says which. See `_replace_punctuation` for the ways it can be
    settled; `strict` withdraws the weakest of them, the bare count."""
    at = [i for i, ch in enumerate(text) if ch == old]
    if not at:
        return None
    if word:
        # The mark this line's target word follows — and only that one. A note
        # that names the next word has named its own mark, however many others
        # the line holds.
        named = [i for i in at
                 if re.match(_QUOTES_THEN + re.escape(word) + r"\b",
                             text[i + 1:], re.IGNORECASE)]
        if len(named) == 1:
            return named[0]
        return None
    if new == "," and old in _TERMINALS:
        # A terminal mark that ends a line of dialogue and runs it into its
        # attribution — "…again.” I said". "Replace period with comma" on such a
        # line means that mark, the one before the closing quote, even when the
        # line holds other sentence periods; picking one of those instead splices
        # two clauses and leaves the tag period the note was written for standing.
        # The tell is the closing double quote and a following tag word, and it is
        # certain only when exactly one mark on the line carries it.
        tagged = [i for i in at if _DIALOGUE_TAG_AFTER.match(text[i + 1:])]
        if len(tagged) == 1:
            return tagged[0]
    if new == "–" and old == "-":
        # An en dash is the mark between the halves of a range — a score, a span
        # of pages or years. A hyphen joining two words is a different animal
        # entirely, and "tap-in" standing next to "1-0" is the shape that got the
        # dash put on the wrong one.
        score = _score_hyphen(text)
        if score is not None:
            return score
    if len(at) == 1 and not strict:
        return at[0]
    return None


def _score_hyphen(text: str) -> int | None:
    """The offset of the one hyphen in `text` standing between two digits, or None
    when there is none or more than one — a score, a span of pages, a span of
    years: the only thing anybody asks an en dash for."""
    at = [i for i, ch in enumerate(text)
          if ch == "-" and text[i - 1:i].isdigit() and text[i + 1:i + 2].isdigit()]
    return at[0] if len(at) == 1 else None


def _swap_mark(text: str, at: int, new: str, word: str = "") -> str | None:
    """`text` with the single character at `at` replaced by `new`.

    A dash is closed up as it goes in: the house sets both dashes with no space
    around them, and a comma swapped for an em dash leaves the space that followed
    the comma behind — "Exactly— what else", which is the one shape in the book
    that is not house style. And when the note named a word to capitalize, it is
    the copy after this mark that is capitalized, not the first one in the span."""
    out = text[:at] + new + text[at + 1:]
    end = at + len(new)
    if new in _CLOSED_UP:
        after = end + (len(out[end:]) - len(out[end:].lstrip(" \t")))
        before = len(out[:at].rstrip(" \t"))
        out = out[:before] + out[at:end] + out[after:]
        end -= at - before
    if word:
        hit = re.search(rf"\b{re.escape(word)}\b", out[end:], re.IGNORECASE)
        if not hit:
            return None
        start = end + hit.start()
        out = out[:start] + out[start].upper() + out[start + 1:]
    return out


def _remove_punctuation(low, note, anchor, highlighted) -> Resolved | None:
    """`Remove comma`, `Remove single quotes`."""
    m = re.match(r"^(?:remove|delete|drop|omit)\s+(?:the\s+)?([a-z ]+?)\s*$", low)
    if not m:
        return None
    name = m.group(1).strip()
    mark = PUNCTUATION.get(name)
    if mark is None:
        return None
    if name.endswith("s") and mark in _OPENERS:
        span = _outer_pair(anchor, mark)
        if span is None:
            return None
        start, end = span
        return _focus(anchor, anchor[:start] + anchor[start + 1:end]
                      + anchor[end + 1:], "remove-punctuation")
    if anchor.count(mark) != 1:
        return None
    return _focus(anchor, anchor.replace(mark, ""), "remove-punctuation")


def _outer_pair(anchor: str, closer: str) -> tuple[int, int] | None:
    """The offsets of the quotation marks that open and close `anchor`, or None
    when it does not hold exactly one unambiguous pair.

    Only the outermost two are the reviewer's quotation marks. Everything between
    them is the quoted text, and in this book — in any book — that text is full of
    the *same character*: a closing single quote and an apostrophe are one and the
    same, so "remove the single quotes" around “For fuck’s sake” had been removing
    the possessive too and shipping "For fucks sake" as a mechanical edit no one
    was asked to look at. Taking the first opener and the last closer, and nothing
    in between, is what tells the reviewer's marks from the author's."""
    opener = _OPENERS[closer]
    if anchor.count(opener) != 1:
        return None                    # no pair, or two quoted runs — ambiguous
    start = anchor.find(opener)
    # The closing mark, told from an apostrophe by what follows it: a quotation
    # closes before a space or a stop, an apostrophe before a letter ("aren’t",
    # "y’all"). Without this the last apostrophe in an unclosed span passes for the
    # close — and a quotation running on to the next line is marked one line at a
    # time, so "‘If you aren’t going to speak to" would have been read as a pair
    # and written back as "“If you aren”t".
    end = -1
    for i in range(len(anchor) - 1, start, -1):
        if anchor[i] == closer and not anchor[i + 1:i + 2].isalnum():
            end = i
            break
    if end <= start:
        return None                    # opens but never closes inside the mark
    return start, end


def _add_punctuation(low, note, anchor, highlighted) -> Resolved | None:
    """`Add comma after`, `Add period after` — the mark goes at the end of the
    marked word. Needs a marked target, so a sticky note's whole line declines."""
    if not highlighted:
        return None
    m = re.match(r"^(?:add|insert)\s+(?:an?\s+|the\s+)?([a-z ]+?)"
                 r"(?:\s+after)?\s*$", low)
    if not m:
        return None
    mark = PUNCTUATION.get(m.group(1).strip())
    if mark is None:
        return None
    find = anchor.strip(_EDGE)
    if not find or find.endswith(mark):
        return None
    # "after" means after the marked words, so the mark has to end where they do.
    # A span of several words, or one that runs past a full stop into the next
    # sentence, has overshot the target and the end of it is the wrong place — a
    # vocative comma asked for after "Sure" must not land after "Sure.” I was".
    if len(find.split()) > 3 or re.search(r"[.!?][\s”’\"']", find):
        return None
    return Resolved(find, find + mark, MECHANICAL, "add-punctuation")


def _case_change(low, note, anchor, highlighted) -> Resolved | None:
    """`Lowercase`, `Capitalize` — the marked word's first letter."""
    if not highlighted:
        return None
    if low in ("lowercase", "lower case", "lc", "lowercase this"):
        want = str.lower
    elif low in ("capitalize", "capitalise", "cap", "uc", "capitalize this",
                 "capitalise this"):
        want = str.upper
    else:
        return None
    find = anchor.strip(_EDGE)
    # "Lowercase" names no word, so the mark has to be *on* the word. Over any
    # longer span the first letter is a guess, and it is the wrong one as often as
    # not — a "Capitalize" on a line that opens mid-sentence means a word further
    # in, and one on a hyphenation fragment ("hibition") means nothing at all.
    if not find or len(find.split()) != 1:
        return None
    for i, ch in enumerate(find):
        if ch.isalpha():
            replace = find[:i] + want(ch) + find[i + 1:]
            return (None if replace == find
                    else Resolved(find, replace, MECHANICAL, "case-change"))
    return None


def _named_capital(low, note, anchor, highlighted) -> Resolved | None:
    """`Capital T` — the note names the letter, so the word is not in doubt even
    when the span holds several.

    It is the *word starting* with that letter, not any occurrence of it: a note
    reading "Capital T" against "the tournament" is asking for one of those two
    words, and counting bare letters would find four t's and mean nothing. Two
    candidate words is still a choice the note does not make, so it declines."""
    m = re.match(r"^capital\s+([a-z])\s*$", low)
    if not m:
        return None
    letter = m.group(1)
    find = anchor.strip(_EDGE)
    starts = [w.start() for w in re.finditer(rf"\b{letter}", find, re.IGNORECASE)]
    if len(starts) != 1:
        return None
    at = starts[0]
    if find[at] == letter.upper():
        return None
    return Resolved(find, find[:at] + letter.upper() + find[at + 1:],
                    MECHANICAL, "named-capital")


def _hyphenate(low, note, anchor, highlighted) -> Resolved | None:
    """`Hyphenate` — the space between the two marked words becomes a hyphen."""
    if low not in ("hyphenate", "hyphen", "hyphenate this", "add hyphen"):
        return None
    find = anchor.strip(_EDGE)
    if find.count(" ") != 1:
        return None
    return Resolved(find, find.replace(" ", "-"), MECHANICAL, "hyphenate")


def _enclose_in_quotes(low, note, anchor, highlighted) -> Resolved | None:
    """`Enclose in quotation marks` — the marked span, quoted.

    A span the author already set in single quotes is *converted*, not wrapped: a
    reviewer asking for quotation marks around a nested quotation means the marks
    it has become doubles, and wrapping instead nests one pair inside the other
    and prints ‘“like this”’. The find string that omitted the singles was what
    let that through."""
    if not highlighted:
        return None
    if not re.match(r"^enclose\s+in\s+(?:double\s+)?(quotes|quotation\s+marks)"
                    r"\s*$", low):
        return None
    find = anchor.strip(_EDGE)
    if not find:
        return None
    if find[0] in "“\"":
        return None                    # already carries the marks it is asking for
    if find[0] in "‘'":
        closer = "’" if find[0] == "‘" else "'"
        if not find.endswith(closer) or find.count(find[0]) != 1:
            return None                # not one plain pair — leave it to a person
        return _focus(find, "“" + find[1:-1] + "”", "enclose-in-quotes")
    return Resolved(find, f"“{find}”", MECHANICAL, "enclose-in-quotes")


def _quote_style(low, note, anchor, highlighted) -> Resolved | None:
    """`Replace single quotes with doubles` — the pair the reviewer marked.

    The singular phrasing is the same request: a reviewer marking a quotation one
    mark at a time writes "replace single quote with double" twice, once on the
    opener and once on the closer, and both mean the pair around them."""
    if not re.match(r"^(?:replace|change)\s+single\s+quotes?\s+with\s+"
                    r"double(?:s|\s+quotes?)?\s*$", low):
        return None
    # The outermost pair, and only it. A closing single quote and an apostrophe are
    # the same character, so a span holding "‘LET’S GO’" has three of them and only
    # two are the reviewer's; converting all three would write "“LET”S GO”". This
    # used to decline such a span altogether, which is most quoted dialogue there
    # is — the contraction is the rule, not the exception.
    span = _outer_pair(anchor, "’")
    if span is None:
        return None
    start, end = span
    replace = anchor[:start] + "“" + anchor[start + 1:end] + "”" + anchor[end + 1:]
    return _focus(anchor, replace, "quote-style")


def _quoted_proposal(low, note, anchor, highlighted) -> Resolved | None:
    """`Should this be "not at all"?` — a question, so a person still decides, but
    the text it proposes is right there in quotes. Carried as a judgment edit with
    the proposal filled in, which is strictly more use to an editor than the
    "the model proposed no concrete change" it used to become."""
    if not highlighted or "?" not in note:
        return None
    quoted = re.findall(r'"([^"]{1,60})"|“([^”]{1,60})”', note)
    if len(quoted) != 1:
        return None
    literal = (quoted[0][0] or quoted[0][1]).strip()
    find = anchor.strip(_EDGE)
    if not literal or not find or literal == find:
        return None
    return Resolved(find, literal, JUDGMENT, "quoted-proposal")


def _bare_replacement(low, note, anchor, highlighted) -> Resolved | None:
    """A note that is simply the corrected text — `wouldn't`, `all right`,
    `twenty-five`, `’til`. What a copy editor writes when the fix is obvious, and
    a third of the notes on a real proof.

    Gated hard, because "the note is the answer" is only true when the note is not
    an instruction and not prose: a marked target, both sides short, no
    instruction verb, no question."""
    if not highlighted or note.endswith("?") or len(note.split()) > 4:
        return None
    if any(v in low for v in _VERBS):
        return None
    if not re.fullmatch(r"[\w’'\-–—.,!\" ]+", note):
        return None
    if not any(ch.isalnum() for ch in note):
        return None
    find = _target_of(anchor, note)
    if find is None or find.lower() == note.lower():
        return None                    # nothing close enough, or a mere quotation
    return Resolved(find, note, MECHANICAL, "bare-replacement")


# --- helpers ------------------------------------------------------------------

# How like the note a run of the marked span has to be for the note to be read as
# a correction *of that run*. A copy editor writing "all right" against "alright"
# is rewriting one word, not the line it sits on, and replacing the whole span
# with the note would delete the rest of the line. So the run is identified by
# similarity, and a note that resembles nothing in the span is declined.
TARGET_FLOOR = 0.5
# The longest run, in words, a short note is allowed to be correcting. "all right"
# replaces one or two words; nothing a four-word note rewrites is safe to guess.
TARGET_WORDS = 3


def _compare(s: str) -> str:
    """A run reduced to what a comparison should care about: letters and digits,
    folded. `alright.` and `Alright` compare equal, and the quotes and spacing the
    PDF reader may have invented drop out."""
    return "".join(c.lower() for c in s if c.isalnum())


def _target_of(anchor: str, literal: str) -> str | None:
    """The run of `anchor` that `literal` is a correction of, or None.

    Every run of up to `TARGET_WORDS` words is scored against the note, and the
    closest is taken if it is close enough. This is what keeps "all right" from
    replacing the whole line it was marked on — and what lets a note land on the
    word it means when the marked span reaches past it."""
    want = _compare(literal)
    if not want:
        return None
    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", anchor)]
    best_key, best = (TARGET_FLOOR, 0), None
    for i in range(len(tokens)):
        for j in range(i, min(i + TARGET_WORDS, len(tokens))):
            start, end = tokens[i][0], tokens[j][1]
            run = _compare(anchor[start:end])
            score = SequenceMatcher(None, run, want).ratio()
            # A correction of a word almost always keeps the word's opening letter.
            # Where it does not, the match is more likely a fragment the marked span
            # happens to share — "backup" against a span truncated to "up the" would
            # otherwise rewrite "up" and produce "back backup the". Declining costs
            # nothing; the model still sees the note.
            if not run or run[0] != want[0]:
                continue
            # On a tie the shorter run wins: a note rewrites a word, not the phrase
            # around it.
            key = (score, -(end - start))
            if key > best_key:
                best_key, best = key, (start, end)
    if best is None:
        return None
    return anchor[best[0]:best[1]].strip(_EDGE) or None

def _strip_capitalize(low: str) -> str:
    """The note without a trailing `and capitalize "she"` clause, so the
    punctuation rules can read the part that names the marks."""
    return re.sub(r"\s+and\s+capitali[sz]e\s+.*$", "", low).strip()


def _capitalize_word(low: str) -> str:
    """The word a `… and capitalize "she"` clause names, or "".

    Worth more than the capitalization it asks for: the word says *which* mark
    the note is about — the one it comes directly after — which is the only thing
    that tells two commas on a line apart."""
    m = re.search(r'and\s+capitali[sz]e\s+["“\'‘]?([a-z]+)', low)
    return m.group(1) if m else ""


# A note may show the character it is naming, or end in a full stop: "Replace with
# en dash (–)." is the same instruction as "Replace with en dash", and reading it
# as neither was how a score kept its hyphen.
_NOTE_TAIL = re.compile(r"\s*\([^)]*\)\s*$")


def _note_core(low: str) -> str:
    """A note without a trailing parenthetical or full stop — the instruction
    itself, in the shape the rules match."""
    core = low.strip()
    while True:
        shorter = _NOTE_TAIL.sub("", core).strip().rstrip(".").strip()
        if shorter == core:
            return core
        core = shorter


# The shortest `find` a rule will emit. A minimal span is the right thing to
# *write* — it cannot disturb a neighbour, and two marks on one line do not
# collide — but it is the wrong thing to *locate*: the minimal span of "replace
# this comma with a period" is "," and a novel holds seven thousand of those. So a
# rule narrows to the change and then widens back out until the span is long
# enough to be an address.
MIN_FIND = 12


def _focus(find: str, replace: str, rule: str) -> Resolved:
    """The edit narrowed to the run that actually changes, then widened until the
    `find` is long enough to locate. Both sides keep the same window, so the
    replacement stays a character-for-character rewrite of the text it replaces and
    cannot drop a word."""
    pre = 0
    while pre < len(find) and pre < len(replace) and find[pre] == replace[pre]:
        pre += 1
    suf = 0
    while (suf < len(find) - pre and suf < len(replace) - pre
           and find[-1 - suf] == replace[-1 - suf]):
        suf += 1
    # Widen alternately, left first, so the span reaches back over the word the
    # mark sat on rather than only forward into the next one.
    while pre + suf > 0 and (len(find) - pre - suf) < MIN_FIND:
        if pre:
            pre -= 1
        elif suf:
            suf -= 1
    return Resolved(find[pre:len(find) - suf], replace[pre:len(replace) - suf],
                    MECHANICAL, rule)
