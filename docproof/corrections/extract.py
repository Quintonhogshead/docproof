"""Extracting an `Edit` list from a free-form corrections source, with a model.

The fuzzy front-end to the deterministic core. An author's list — prose ("change
'harbor' to 'harbour' throughout"), a pasted table, or the text pulled out of a
corrections PDF — is read by a model into the same structured edit list a typed
list would give, then handed straight to `parse.parse_edits` for validation.

The safety argument is the whole reason extraction is a separate stage from
application: the model only *proposes*. Every anchor it emits is checked by
`apply` against the real book — a `find` that is not there is flagged, never
guessed — and `verify` proves the applied result matches the list. So extraction
is allowed to be imperfect without being unsafe. It is also why this returns a
`ParseResult` (edits + issues) for a human to review before anything is applied,
rather than applying anything itself.

Model-agnostic: it takes a `Provider` and reuses the same structured-output path
prep's subject detection does, so it works on whichever model the caller wired.
"""
from __future__ import annotations

import re
from typing import Callable, Literal, Sequence

from pydantic import BaseModel

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .model import DESIGN, PARA_MERGE_NEXT
from .parse import ParseResult, parse_edits
from .textmatch import normalize

MAX_OUTPUT_TOKENS = 8000


class ExtractionError(RuntimeError):
    """The model would not, or could not, return a usable edit list. Distinct
    from a parse issue, which is a *single* entry the model got wrong — this is
    the whole call failing (a refusal, a truncation, no answer)."""


# The schema the model fills. Every field is required on the wire (see
# strict_json_schema), but the defaults document what an unmarked field means, and
# parse_edits — not the schema — is what finally normalizes and validates them, so
# a model that puts an odd kind or a stray occurrence is corrected or flagged
# there rather than crashing the call.
class _ExtractedEdit(BaseModel):
    find: str
    replace: str
    context: str = ""
    instruction: str = ""
    kind: Literal["mechanical", "judgment", "design"] = "mechanical"
    occurrence: int = 0
    source: str = ""
    format: Literal["", "italic", "roman", "swash", "no-swash"] = ""
    paragraph: Literal["", "recto", "verso", "page-break", "column-break",
                       "keep-with-next", "keep-together", "allow-break",
                       "no-page-break", "delete-paragraph", "insert-after",
                       "insert-before", "merge-next", "split-at"] = ""


class _Extracted(BaseModel):
    edits: list[_ExtractedEdit]


_SYSTEM = """\
You convert a list of corrections for a book into a precise, machine-applicable \
edit list. Each correction becomes one edit:

- find: the exact text in the book to change, copied VERBATIM — same words, \
same punctuation, same capitalization. This is how the edit is located, so it \
must be text that actually appears in the book, not a paraphrase. WHERE THE \
SOURCE GIVES YOU THE BOOK'S OWN TEXT for a page, copy the find out of that and \
nowhere else: it is the text the edit is applied to, and an anchor that is not in \
it character for character cannot be located at all. The highlighted text quoted \
with a comment is the PDF's rendering of the same words, so it may be hyphenated \
at a line end, carry a space the typesetting introduced, or stop mid-word — read \
it to see WHICH words the comment is about, then quote those words from the book \
text. Where no book text is given, repair obvious extraction artifacts yourself (a \
stray space inside a word — "Y ou", "hil t", "b efore" — a broken hyphenation) so \
the find reads as the clean text the book contains.
  Quote enough to identify the spot and no more. For a word swap the find is ONLY \
the word(s) that change, never the words around them: "change quickly to swiftly" \
is find "quickly", replace "swiftly"; it is NOT find "moved quickly", replace \
"moved swiftly", and NEVER find "quickly through" or "slowly eased" when only one \
of those words is being changed — carrying an unchanged neighbour into find is how \
a verb or object gets dropped. But a find of one or two characters is not an \
anchor: NEVER emit a find that is a bare "," or "." or a single quote mark. A \
punctuation change takes the few words around the mark, so the find can be found — \
find "he said, and then", replace "he said. And then", not find ",".
  NEVER PUT PUNCTUATION IN find THAT YOU HAVE NOT SEEN IN THE BOOK. A note that \
asks you to PLACE or ADD a mark ("please put a ? at the end of that sentence") is \
telling you the mark is NOT there — the find is the words alone and the replace \
is those same words with the mark added: find "your cowardice", replace "your \
cowardice?", never find "your cowardice." on the assumption a full stop is \
sitting there. Only a note that asks you to REPLACE or CHANGE an existing mark \
("change the semicolon after 'you' to a period") tells you the mark is in the \
book, and only then does find carry it. This matters most in verse, where a line \
routinely ends with no punctuation at all.
- replace: the corrected text. Use an empty string for a pure deletion. For an \
insertion, set find to the existing text around the insertion point and put that \
same text plus the new words in replace.
- context: when find is a short or common phrase that may appear more than once \
in the book, set context to a longer VERBATIM run of the surrounding text (the \
sentence or line the correction is attached to) that contains find exactly once. \
The edit is then located inside this context, so it lands on the intended spot — \
the one the mark was on — not another copy elsewhere. Leave context empty when \
find is already unique. Copy it from the book text for the page, the same as find.
  WHEN THE NOTE ITSELF QUOTES THE LINE — "capitalize the S in 'siren's': 'And \
resist the pull of a Siren's locomotive.'" — that quote IS the context: copy it \
in, even if you think find is unique. It is the reviewer telling you which copy \
they marked, and it is the only thing that can tell two copies apart in a book \
you cannot see. Quote it as the book has it where you have the book's text; \
otherwise copy the note's quote as written, with the correction it shows undone \
if you can (the reviewer often quotes the line already corrected).
- instruction: the human note the correction came from, verbatim, for the report.
- format: "italic" or "roman" when the correction is about how the text is SET \
rather than what it says — "italicize this film title", "de-italicize". Also \
"swash" when the note asks for a decorative or flourished letterform — "swoop the \
R", "swash the S", "make this match the other swash capitals" — and "no-swash" to \
take one off. Put the words to style in find (for a swash, the WHOLE WORD holding \
the letter, never the bare letter: a find of "R" is not an anchor, and the swash \
only changes the letters the font has a flourished form for), copy them unchanged \
into replace, and leave kind "mechanical": this is an appliable edit, not a design \
request. Use "" for every ordinary correction.
- paragraph: a whole-paragraph request, when the note is about the paragraph \
rather than its words. "recto"/"verso" to start it on a right/left-hand page, \
"page-break" to start it on a new page, "keep-with-next" so a heading is not left \
at the foot of a page, "keep-together" so it does not split, "delete-paragraph" to \
remove it, "insert-after"/"insert-before" to add one (put the new text in replace). \
Two more are about where the breaks fall, which is what most notes on a proof of \
poetry are asking about, because every line of verse is its own paragraph:
  "merge-next" — "delete the line break between X and Y", "let this run on as one \
paragraph". Put in find a verbatim run of the book that REACHES ACROSS the break \
being deleted: the words on both sides of it, e.g. find "dead people stuff: \
Rotting". The note's own words are not the book's: "delete the line break between \
"study" and "spelunking"" gives find "study spelunking" — the two sides as they \
meet once the break is gone — NEVER "study and spelunking", which carries the \
note's connective into an anchor and matches nothing. One break per edit — if the \
note asks for several line breaks to go, emit one merge-next edit per break, in \
order.
  "split-at" — "insert a paragraph break here", "start a new paragraph at X". Put \
in find ONLY the text that should START the new paragraph, e.g. find "The next \
hour or so"; the break is made immediately in front of it. Never make find reach \
back into the text that stays in the first paragraph.
  For both, copy find unchanged into replace. \
Put the words that identify the paragraph in find, copy them unchanged into \
replace, and leave kind "mechanical" — these are appliable. NEVER read one of these \
out of a question ("should we cut this?"): that is a "judgment".
- paragraph_style: the full style name to apply to that paragraph, e.g. \
"ParagraphStyle/Block Quote". Only when the note names a style that the book \
already uses; do not invent one.
- kind: "mechanical" for a clear text change with one right answer; "judgment" \
when a person should decide (a vague or stylistic note, or one you cannot turn \
into an exact find/replace); "design" ONLY for something about how the page \
composed — a bad line break, a widow, a loose rag, a page that runs long. Those \
cannot be applied by anyone but InDesign, so anchor the note to the line it was \
marked on and it becomes a located check for the designer. Italics are NOT design \
(use "format"), and neither is where a chapter starts (use "paragraph").
- occurrence: 0 when the text should be unique, or the 1-based Nth when the \
correction names a specific instance among several.
- source: when the item carries an id (e.g. "id p3-2"), copy that id here so the \
edit can be traced back to the comment it came from. Leave empty if there is no id.

Rules:
- Extract only corrections that are actually stated. Never invent one.
- Emit exactly one edit for EVERY item, in order, and never drop one — not even a \
terse or query-like note ("corridor?", "minute", "gray?"). If you cannot pin the \
exact text to change, still emit it as kind "judgment" with the note in \
instruction and a find of the anchored line if you have one, so a human sees it; \
do not guess a location and do not silently omit the item.
- One edit per correction. Do not merge or split them."""


# --- repairing the three anchors a model writes from the note, not the book ----
#
# The rules above tell the model not to make these mistakes. This is what catches
# the ones it makes anyway, deterministically, before the list ever reaches a
# person to review — each repair reads only the note the correction came from and
# the find/replace beside it, and each is narrow enough to be wrong about nothing
# else. They matter most on the source that gives the model no book text at all: a
# prose list of notes, where every anchor is the model's guess at how the book
# spells what the reviewer described.

# A run the note quotes — the reviewer's own rendering of the book. Both quote
# styles, and short of a paragraph: a "quote" longer than this is prose about the
# correction, not a line lifted out of the book.
_QUOTED = re.compile(r"[“\"]([^“”\"]{2,400})[”\"]")

# How much more than the anchor a quoted run has to carry before it counts as the
# line the anchor sits in. A note quotes the words it is changing at least as
# often as it quotes their sentence ("replace “dismay” with “consternation”"), and
# that quote is not a context — it is the edit itself, and pinning the edit inside
# a copy of itself narrows nothing while risking the wrong copy.
_CONTEXT_EXTRA_WORDS = 3

_SENTENCE_MARKS = ".?!,;:"
# "Place a ? at the end" says the mark is absent; "change the . to a ?" says it is
# there. The two verbs are what tell an insertion from a swap, and a note carrying
# either kind of swap word is left alone.
_ADD_VERB = re.compile(r"\b(place|add|insert|put)\b", re.IGNORECASE)
_SWAP_VERB = re.compile(r"\b(replace|replacing|change|changing|swap|delete|"
                        r"remove|instead of|take out)\b", re.IGNORECASE)


def _quoted_runs(note: str) -> list[str]:
    """The runs the note quotes, in order, as the reviewer wrote them."""
    return [m.group(1).strip() for m in _QUOTED.finditer(note or "")]


def _same(a: str, b: str) -> bool:
    """The two anchors are the same text once spelling of quotes, dashes,
    spacing and case are set aside — the comparison `apply` itself would make."""
    return normalize(a, fold_case=True) == normalize(b, fold_case=True)


def _repair_break_anchor(entry: dict) -> None:
    """A merge-next anchor rebuilt from the two sides of the break, when the model
    copied the note's phrasing instead.

    "Delete the line break between “study” and “spelunking”" wants an anchor that
    reaches across the break — "study spelunking" — and what comes back is often
    the note's own sentence, "study and spelunking", whose "and" is nowhere in the
    book. Only that exact shape is rewritten: the find must be the two quoted
    sides joined by the note's connective and nothing else, so an anchor the model
    did quote from the book is never touched."""
    if entry.get("paragraph") != PARA_MERGE_NEXT:
        return
    find = entry.get("find") or ""
    quoted = _quoted_runs(entry.get("instruction") or "")
    for left, right in zip(quoted, quoted[1:]):
        if _same(find, f"{left} and {right}"):
            joined = f"{left} {right}"
            if _same(entry.get("replace") or "", find):
                entry["replace"] = joined   # a break edit rewrites no words
            entry["find"] = joined
            return


def _repair_added_terminal_mark(entry: dict) -> None:
    """The punctuation an "add a mark" note never said was there, dropped from the
    find.

    A note reading "please place a ? at the end of that sentence" is an insertion:
    the book has the words and no mark. A model that has not been shown the book
    tends to write the sentence out with a full stop anyway and swap that for the
    question mark, and the full stop it invented is what stops the anchor matching
    — a verse line, which is where these notes mostly land, usually ends bare.

    So a find and replace differing only in a final sentence mark, on a note that
    asks to *place* one and never mentions an existing one, become the insertion
    the note described. If the book does turn out to carry a mark there, `apply`
    absorbs it rather than doubling it (`_absorb_stale_terminal`)."""
    find, replace = entry.get("find") or "", entry.get("replace") or ""
    if entry.get("paragraph") or entry.get("format") or entry.get("kind") == DESIGN:
        return
    if len(find) < 2 or len(replace) < 2 or find[-1] == replace[-1]:
        return
    if find[-1] not in _SENTENCE_MARKS or replace[-1] not in _SENTENCE_MARKS:
        return
    if find[:-1] != replace[:-1]:
        return
    note = entry.get("instruction") or ""
    if not _ADD_VERB.search(note) or _SWAP_VERB.search(note):
        return
    entry["find"] = find[:-1]
    entry["replace"] = find[:-1] + replace[-1]


def _lift_context_from_note(entry: dict) -> None:
    """The line the reviewer quoted, lifted onto the edit as its context anchor.

    A note that quotes the sentence it was written against has already said which
    copy of a common word it means; leaving that in the prose of the instruction
    throws the answer away, and a `find` of "siren’s" in a book with two of them
    is then flagged as ambiguous with the disambiguation sitting right there in
    the note. The quote is only ever a *narrowing*: one that does not match the
    book is dropped by `apply` and the edit is located exactly as it would have
    been, so lifting it can lose nothing.

    Only a quote that is materially longer than the find and contains it (or the
    corrected form the reviewer usually quotes) is taken — the note's quote of the
    changed word itself is not a line."""
    if (entry.get("context") or "").strip() or entry.get("kind") == DESIGN:
        return
    find, replace = entry.get("find") or "", entry.get("replace") or ""
    if not find:
        return
    probe = normalize(find, fold_case=True)
    corrected = normalize(replace, fold_case=True)
    if not probe:
        return
    best = ""
    for run in _quoted_runs(entry.get("instruction") or ""):
        view = normalize(run, fold_case=True)
        if probe not in view and not (corrected and corrected in view):
            continue
        anchor = find if probe in view else replace
        if len(run.split()) - len(anchor.split()) < _CONTEXT_EXTRA_WORDS:
            continue                        # the word again, not the line it sits in
        if len(view) > len(normalize(best, fold_case=True)):
            best = run
    if best:
        entry["context"] = best


def repair_entries(parsed):
    """The model's edit list with the three anchors it writes from the note rather
    than the book repaired. Takes and returns the shape the schema produces (an
    object with an "edits" list); anything else is passed through untouched, since
    `parse_edits` owns what a valid source looks like."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("edits"), list):
        return parsed
    entries = []
    for entry in parsed["edits"]:
        if isinstance(entry, dict):
            entry = dict(entry)
            _repair_break_anchor(entry)
            _repair_added_terminal_mark(entry)
            _lift_context_from_note(entry)   # last: it reads the repaired find
        entries.append(entry)
    return {**parsed, "edits": entries}


def extract_edits(source_text: str, provider: Provider, *, model: str,
                  usage: Usage, max_tokens: int = MAX_OUTPUT_TOKENS
                  ) -> ParseResult:
    """Read a free-form corrections source into a `ParseResult`.

    `usage` accrues the model call's tokens. Raises `ExtractionError` if the
    call itself fails (refusal, truncation, no answer); per-entry problems come
    back as `ParseResult.issues`, not exceptions."""
    if not source_text.strip():
        return ParseResult(edits=(), issues=())
    result = provider.complete_structured(
        model=model, system=_SYSTEM, user=source_text,
        schema=strict_json_schema(_Extracted), schema_name="corrections",
        max_tokens=max_tokens)
    usage.add(result.usage, model=model)
    if result.stop_reason != "ok" or result.parsed is None:
        raise ExtractionError(
            result.error or result.stop_reason or "no answer from the model")
    # parse_edits owns the "flag, never guess" contract and accepts the
    # {"edits": [...]} wrapper the schema produces, so validation lands in one
    # place whether a list was typed or extracted. The repair pass runs first, so
    # what a person reviews in the panel is the repaired anchor — not one that
    # will silently fail to match hours later, when the file is being corrected.
    return parse_edits(repair_entries(result.parsed))


def extract_edits_batched(sources: Sequence[str], provider: Provider, *,
                          model: str, usage: Usage,
                          max_tokens: int = MAX_OUTPUT_TOKENS,
                          progress: Callable[[int, int, ParseResult], None]
                          | None = None) -> ParseResult:
    """Extract several bounded corrections sources in turn, concatenated into one
    `ParseResult`. Each source is read by its own model call, so no single call
    can overrun the output ceiling and truncate — the failure mode that silently
    dropped every edit when a large proof was read in one shot.

    Edits keep their source order (batch 1's edits, then batch 2's, …), which is
    the order the reviewer expects. `usage` accrues across all calls. A batch
    that fails raises `ExtractionError` as the single-call form does — the caller
    decides whether to surface a partial result; the batches read so far are not
    lost, because the caller drives the loop when it wants per-batch recovery.
    `progress(done, total, cumulative)` is called after each batch."""
    all_edits: list = []
    all_issues: list = []
    total = len(sources)
    for i, src in enumerate(sources, 1):
        result = extract_edits(src, provider, model=model, usage=usage,
                               max_tokens=max_tokens)
        all_edits.extend(result.edits)
        all_issues.extend(result.issues)
        if progress is not None:
            progress(i, total,
                     ParseResult(edits=tuple(all_edits), issues=tuple(all_issues)))
    return ParseResult(edits=tuple(all_edits), issues=tuple(all_issues))
