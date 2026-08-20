"""The last tier: a frontier model, given the book rather than the line.

`secondlook` settles the queries a reviewer *delegated* — "em dash or semicolon",
an if-spoken-aloud conditional — and declines the rest. What it declines is not
a harder decision so much as a question it was not given enough to answer. Look
at what actually survives:

  "Should 'the' be removed as in previous instances of Winn-Dixie?"
  "Single period or an ellipsis?"
  "Confusing — should this be deleted, or should 'I' be added before 'locked'?"
  "Unsure if there's a typo here"

None of those needs authorial intent. The first is a consistency lookup and the
reviewer even states the rule — match the other instances — which is a fact about
the book, sitting in the book, that nothing in the pipeline had ever gone and
read. The second is settled by the passage plus the house's own typography. The
third is settled by whether the surrounding narration is first person. The
fourth is settled by how the character speaks elsewhere. Every one of them is a
question about *the book*, and the pass that declined them had been shown a page.

So this tier changes the input, not just the model. Each query goes up with:

  * the passage around the mark — the paragraphs either side of the page, not the
    page alone, which is what settles a reading that depends on who is speaking;
  * evidence gathered deterministically from the whole book — every occurrence of
    the terms the note names, with their surroundings, so "as in previous
    instances" is a list the model can count rather than a memory it must have;
  * the house's typographic rules, so a choice between a period and an ellipsis
    is made against the same conventions the review pass enforces.

And it may answer in two ways, which is the other half of the design. It
*resolves* a query only when the text in front of it settles the matter — a
consistency rule the book plainly follows, a mark the house sets one way. When
the answer brushes against what the author meant, it does not resolve: it
*recommends*, and the query stays flagged for a person, arriving with the reading
and the evidence attached (`Edit.advice`). That is the honest split. A tier that
resolved everything would be guessing at intent under cover of a bigger model; a
tier that resolved nothing would leave the reviewer to redo the reading by hand.

Every resolution is guarded exactly as the tiers below it are: the find must be
copied verbatim out of the passage it was shown, so an anchor is quoted and never
recalled, and what it produces then anchors against the real book in `apply`, is
read by the sanity gate, and is named in the change log.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace as _replace
from typing import Callable, Literal, Sequence

from pydantic import BaseModel

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .apply import all_spans
from .idml import Story
from .model import DESIGN, Edit, MECHANICAL
from .secondlook import MIN_FIND, is_query
from .textmatch import IndexCache

log = logging.getLogger("docproof.corrections.escalate")

# Generous, because a frontier model's thinking is billed against this ceiling
# and a structured reply that runs past it parses as nothing at all — a silent
# loss of the whole answer. One query per call keeps the reply small anyway.
MAX_OUTPUT_TOKENS = 16000
# How many paragraphs either side of the marked page to send. Enough to see who
# is speaking and how the scene runs; short of the whole chapter, which would
# cost more than it settles.
PASSAGE_RADIUS = 10
# And a hard ceiling on the passage, for a book of very long paragraphs.
PASSAGE_CHARS = 8000
# Whole-book evidence: how many terms to research per query, how many occurrences
# of each to show, and how much text around each.
MAX_TERMS = 3
MAX_HITS = 14
HIT_BEFORE, HIT_AFTER = 48, 32
# Above this many queries the tier stops and says so. A proof whose every mark is
# a question is a proof to read by hand, not to spend a frontier model on; the
# count that is dropped is logged rather than quietly trimmed.
MAX_QUERIES = 40

# The house's typographic conventions, as the deterministic sweeps enforce them
# (see docproof.sweeps). Sent with every query so a choice between two marks is
# made against the same rules the review pass would apply to the result.
HOUSE_STYLE = """\
- Em dash: unspaced — like this — never spaced or doubled.
- Ellipsis: the single character …, with a non-breaking space before it when it \
attaches to the preceding word or mark.
- Quotation marks are curly (“ ” ‘ ’), never straight.
- Periods and commas sit inside a closing quotation mark.
- Chicago style, spelled-out numbers under 101."""


class _Verdict(BaseModel):
    verdict: Literal["resolve", "recommend", "leave"]
    find: str = ""
    replace: str = ""
    context: str = ""
    note: str = ""


# A reviewer note that questions a terminal period or a capital — the two marks a
# passage's own convention can settle. "Add period?", "Capitalize?", "should this
# be lowercase". These are the queries the last tier is allowed to *decide* rather
# than defer: the surrounding lines show whether the book punctuates its sentences
# or runs its verse open, whether a word is set as a proper noun elsewhere.
_PERIOD_CAP = re.compile(
    r"\b(period|full[ -]?stop|terminal punctuation|"
    r"capitali[sz]e?d?|capitali[sz]ation|capital|uppercase|upper[ -]case|"
    r"lowercase|lower[ -]case|cap)\b", re.IGNORECASE)


def is_period_cap_query(note: str) -> bool:
    """Whether a note is asking about a terminal period or a capitalization — the
    marks the surrounding text's own convention can decide, so the last tier may
    settle them (add the mark, or confirm the line as deliberately set) instead of
    deferring. A note about a word choice or meaning is not one of these."""
    return bool(note and _PERIOD_CAP.search(note))


# --- gathering the context ----------------------------------------------------

def passage_around(stories: Sequence[Story], scope, page: int, *,
                   radius: int = PASSAGE_RADIUS,
                   max_chars: int = PASSAGE_CHARS) -> str:
    """The book's text around the page a mark sat on — the paragraphs the page
    covers, plus `radius` either side, in reading order.

    The page alone is what every tier below this one is given, and it is the
    reason the questions that reach here cannot be answered: whether "I" belongs
    before a verb is a fact about the narration, and the narration is a property
    of the scene, not the page. Empty when the page was never placed."""
    if scope is None or not page or not scope.knows(page):
        return ""
    spans = scope.span_of(page)
    if not spans:
        return ""
    by_id = {s.story_id: s for s in stories}
    story = by_id.get(spans[0][0])
    if story is None:
        return ""
    here = [para for sid, para, _lo, _hi in spans if sid == story.story_id]
    lo = max(0, min(here) - radius)
    hi = min(len(story.paragraphs) - 1, max(here) + radius)
    out, total = [], 0
    for para in story.paragraphs[lo:hi + 1]:
        text = para.text.strip()
        if not text:
            continue
        if total + len(text) > max_chars and out:
            break
        out.append(text)
        total += len(text)
    return "\n\n".join(out)


# A term worth researching across the book: a name the note or the mark spells
# with an internal capital or a hyphen ("Winn-Dixie", "LimeWire", "McDonald's").
# Those are the words a consistency question is ever about, and the shape is
# distinctive enough that nothing else has to be guessed at.
_DISTINCTIVE = re.compile(r"\b[A-Z][a-z’']*(?:[-’'][A-Z][a-z’']*)+\b|"
                          r"\b[A-Z][a-z]+[A-Z][A-Za-z]*\b")
_QUOTED = re.compile(r"[\"“‘']([^\"“”‘’']{3,40})[\"”’']")


# A reviewer note that names its own fix: "should this be 'lightning'?", "should
# be lightning", "change to lightning". The proposed word is what the reviewer
# already decided; when the book's own usage confirms it, resolving carries out
# their mark rather than deciding what the author meant.
_PROPOSAL = re.compile(
    r"(?:should(?:\s+(?:this|it|that|these|they))?\s+be|should\s+be|"
    r"change\s+to|make\s+it|use)\s*"
    r"[\"“‘']?([A-Za-z][^\"”’'?!.]*?)[\"”’'?!.]*\s*$",
    re.IGNORECASE)


def proposed_replacement(note: str) -> str | None:
    """The specific correction a reviewer's note proposes, or None.

    "Should this be 'lightning'?" and "should be lightning" both name the word the
    reviewer wants — a proposal, not a question of intent. This reads it back out,
    so the last tier can be told the reviewer has already chosen the target and
    need only confirm it against the book. A note that asks without proposing
    ("Add period?", "Capitalize?", "Should we cut this?") yields nothing — those
    stay the author's call."""
    if not note:
        return None
    m = _PROPOSAL.search(note.strip())
    if not m:
        return None
    proposal = " ".join(m.group(1).split())
    # A handful of words are directions, not targets — "should be removed",
    # "should be deleted" name an operation the reviewer wants, not a replacement.
    if proposal.lower() in {"removed", "deleted", "cut", "changed", "different",
                            "corrected", "fixed", "here", "this", "that"}:
        return None
    return proposal or None


def gather_terms(note: str, anchor: str) -> list[str]:
    """The terms a query is asking about, to be researched across the book.

    Two shapes, both deterministic: a distinctively-capitalized name in the note
    or the marked text, and anything the note puts in quotation marks. "Should
    'the' be removed as in previous instances of Winn-Dixie?" yields both — and
    it is the name, not the article, that the book can be searched for."""
    terms: list[str] = []
    for source in (note or "", anchor or ""):
        for m in _DISTINCTIVE.finditer(source):
            if m.group(0) not in terms:
                terms.append(m.group(0))
    for m in _QUOTED.finditer(note or ""):
        q = m.group(1).strip()
        if q and q not in terms:
            terms.append(q)
    return terms


def term_evidence(stories: Sequence[Story], terms: Sequence[str], *,
                  max_terms: int = MAX_TERMS, max_hits: int = MAX_HITS
                  ) -> list[tuple[str, int, list[str]]]:
    """Every occurrence of each term in the whole book, as `(term, count,
    snippets)` — the surroundings of each, so the pattern the book actually
    follows can be read off rather than recalled.

    This is the part no model can do for itself and no page of context can
    supply: "as in previous instances" names evidence that is spread over four
    hundred pages. A term the book carries once tells a consistency question
    nothing, so it is dropped; the rest come back most-repeated first."""
    cache = IndexCache()
    found: list[tuple[str, int, list[str]]] = []
    for term in terms:
        hits: list[str] = []
        count = 0
        for story in stories:
            for para in story.paragraphs:
                for start, end in all_spans(para.text, term, cache=cache):
                    count += 1
                    if len(hits) < max_hits:
                        lo = max(0, start - HIT_BEFORE)
                        hi = min(len(para.text), end + HIT_AFTER)
                        snippet = para.text[lo:hi].strip()
                        hits.append(("…" if lo else "") + snippet
                                    + ("…" if hi < len(para.text) else ""))
        if count > 1:
            found.append((term, count, hits))
    found.sort(key=lambda t: -t[1])
    return found[:max_terms]


def _brief(edit: Edit, passage: str,
           evidence: list[tuple[str, int, list[str]]],
           proposal: str | None = None) -> str:
    """One query, with everything gathered for it."""
    lines = [f"The reviewer's note: \"{edit.instruction}\""]
    if edit.context or edit.find:
        lines.append(f"Marked on: \"{edit.context or edit.find}\"")
    if edit.page:
        lines.append(f"Page of the proof: {edit.page}")
    if is_period_cap_query(edit.instruction):
        lines.append(
            "This mark questions a terminal period or a capitalization — decide it "
            "from the surrounding text's own convention, which the passage above "
            "shows. If comparable lines in the passage take the mark (prose that "
            "punctuates its sentences, a genuine sentence start), RESOLVE and make "
            "the change. If they consistently do without it — an open-line poem "
            "that drops terminal periods, a lowercase the passage keeps throughout "
            "— answer LEAVE: it is deliberate and correct as set, no change needed. "
            "Only RECOMMEND when the passage genuinely mixes both with no rule to "
            "read off. Deciding from a clear convention carries out the reviewer's "
            "check; it does not override the author.")
    if proposal:
        lines.append(
            f"The reviewer has PROPOSED a specific correction: \"{proposal}\". "
            "They have already chosen the target; your job is to confirm it "
            "against the book's own usage, not to decide it afresh. If the "
            "evidence below shows \"" + proposal + "\" is the spelling or word "
            "the book uses elsewhere and this instance is the outlier, RESOLVE "
            "with it — carrying out the reviewer's mark is not deciding the "
            "author's intent. Recommend only if the evidence is absent or the "
            "choice is genuinely the author's voice.")
    if passage:
        lines += ["", "THE BOOK AROUND THAT MARK — copy any find you quote out "
                      "of this, character for character:", "", passage]
    if evidence:
        lines += ["", "HOW THE REST OF THE BOOK SETS THE TERMS THIS NOTE NAMES "
                      "— every occurrence, so a consistency question can be "
                      "settled by counting:"]
        for term, count, hits in evidence:
            lines.append("")
            lines.append(f"\"{term}\" — {count} occurrences in the book:")
            lines += [f"  · {h}" for h in hits]
    lines += ["", "THE HOUSE'S TYPOGRAPHY:", HOUSE_STYLE]
    return "\n".join(lines)


_SYSTEM = """\
You are the most senior editor at a book publisher, and the last person to look \
at a query before it goes back to a proofreader as unanswered. You are given one \
note a reviewer wrote on a proof, the passage of the book around it, every \
occurrence in the book of the terms the note names, and the house's typographic \
rules.

Every query you see has already been through a pass that settles the easy ones, \
so do not expect the answer to be obvious. Read the evidence before you decide.

Answer in one of exactly three ways: "resolve", "leave", or "recommend".

"resolve" — the text in front of you settles the matter, and the change follows \
from evidence rather than from taste. Use it when:
- The note asks about consistency and the occurrences show what the book does \
("as in previous instances of X" — count them; if the book plainly follows one \
pattern, make this instance match it).
- The note asks which of two marks or spellings is correct, and the house's \
rules or the passage decide it (a trailing-off line takes an ellipsis; a broken \
two-dot mark is wrong either way).
- The note offers two repairs and the surrounding prose makes exactly one of \
them grammatical.
- The note PROPOSES a specific word or spelling ("should this be 'lightning'?", \
"should be eaves") and the book's own usage confirms it — the proposed spelling \
is what the book uses elsewhere and this instance is the lone outlier. The \
reviewer named the target, so applying it carries out their mark; you are \
confirming a proofreader's catch against the book, not deciding the author's \
meaning. (Confirm it against the evidence — do not resolve a proposed word the \
book gives no support for.)
Then give the edit:
- find: copied VERBATIM out of the book passage above — same words, punctuation \
and capitalization. Only what changes plus the least surround needed to locate \
it; never a bare punctuation mark.
- replace: the corrected text, set in the house's marks.
- context: a longer verbatim run containing the find, when the find could appear \
more than once in the passage.
- note: one sentence giving the evidence you used ("the book has 'Winn-Dixie' \
without the article in eight of nine instances").

"leave" — for a mark that questions a terminal period or a capitalization, when \
the surrounding text shows the line is correct as set and needs no change: an \
open-line poem that drops terminal periods on comparable lines, a lowercase the \
passage keeps throughout, a word already capitalized the way the book capitalizes \
it. This is the mirror of "resolve": the passage's own convention decides the \
answer, and here the answer is that nothing changes. Use it ONLY for a period or \
capitalization question the convention settles — never to wave off a query about \
a word, a meaning, or anything else; those "recommend". Give:
- note: the convention you read and why it leaves this mark as set, in one \
sentence ("the poem ends four comparable lines without a period, so this open \
line is deliberate").

"recommend" — you have a view, but answering would decide something only the \
author or the editor can. Use it whenever the question touches what the writer \
MEANT: whether a word is a typo or the character's voice, whether a line was \
cut on purpose, whether a repetition is deliberate. Do NOT resolve these, however \
confident you are — a reader's dialect written as dialect is not an error, and \
"corrected" it is a real loss. The one exception is the proposed-and-confirmed \
case above: when the reviewer named the exact fix AND the book's usage confirms \
it, that is a proofreader's catch to carry out, not intent to decide. Absent that \
confirmation, recommend. Then give:
- note: what you found and what you would do, in one or two sentences, written \
for the person who now has to decide. Name the evidence. Say plainly if your \
recommendation is to change nothing.

Never invent a change the note did not ask about. Never quote a find that is not \
in the passage above. When you cannot tell, "recommend" is always available and \
always safe."""


# --- the tier -----------------------------------------------------------------

def _resolved_edit(original: Edit, verdict: dict, passage: str) -> Edit | None:
    """The concrete edit a resolution makes, or None when it fails the guards.
    The find must be verbatim in the passage the model was shown — quoted out of
    the book, never recalled — which is the same check the re-anchor tier
    makes."""
    find = (verdict.get("find") or "").strip("\n")
    rep = verdict.get("replace") or ""
    if len(find.strip()) < MIN_FIND or find not in passage or rep == find:
        return None
    context = (verdict.get("context") or "").strip("\n")
    if context and find not in context:
        context = ""
    note = " ".join((verdict.get("note") or "").split())
    instruction = (original.instruction or "").rstrip()
    if note:
        instruction += f" — resolved on the book: {note}"
    return _replace(original, find=find, replace=rep, context=context,
                    occurrence=0, kind=MECHANICAL, instruction=instruction)


def _left_edit(original: Edit, verdict: dict) -> Edit:
    """The query turned into a recorded no-op: the model read the passage and found
    the mark's target correct as set — a terminal period the open-line verse drops
    on purpose, a lowercase the poem keeps throughout — so the answer is no change.

    `find == replace` makes `apply` file it as a true no change rather than an open
    query, so it clears the needs-a-human list; the reasoning rides on `advice` (and
    so onto the outcome's detail), so the report can show WHY it was left, which is
    the whole of "clear it, but record the reasoning". A no-op is never written to
    the file, so a wrong "leave" costs a mark unmade, never a wrong change shipped."""
    note = " ".join((verdict.get("note") or "").split())
    instruction = (original.instruction or "").rstrip()
    if note:
        instruction += f" — confirmed as set: {note}"
    return _replace(original, find=original.find, replace=original.find,
                    context="", occurrence=0, kind=MECHANICAL,
                    advice=note or original.advice, instruction=instruction)


def escalate_queries(edits: Sequence[Edit], provider: Provider, *, model: str,
                     usage: Usage, stories: Sequence[Story], scope=None,
                     max_tokens: int = MAX_OUTPUT_TOKENS,
                     progress: Callable[[int, int], None] | None = None
                     ) -> tuple[list[Edit], int, int]:
    """The edit list with the surviving queries either resolved into concrete
    edits or annotated with what the model found, plus `(resolved, recommended)`.

    One call per query: each carries its own passage and its own whole-book
    evidence, so there is nothing to gain by batching them and a truncated reply
    would cost every query in the batch rather than one. A call that fails leaves
    its query exactly as it was — flagged, and a person's.

    `progress(done, total)`, when given, is called before each query — this is
    the slowest pass on the list, one frontier call per note, so it is the one
    a card most needs to show moving."""
    out = list(edits)
    at = {e.id: i for i, e in enumerate(out)}
    candidates = [e for e in out if is_query(e) and e.kind != DESIGN]
    if not candidates:
        return out, 0, 0
    if len(candidates) > MAX_QUERIES:
        log.warning("%d queries reached the last tier; reading the first %d and "
                    "leaving %d for a human", len(candidates), MAX_QUERIES,
                    len(candidates) - MAX_QUERIES)
        candidates = candidates[:MAX_QUERIES]

    resolved = recommended = left = 0
    schema = strict_json_schema(_Verdict)
    for n, edit in enumerate(candidates):
        if progress:
            progress(n, len(candidates))
        passage = passage_around(stories, scope, edit.page)
        proposal = proposed_replacement(edit.instruction)
        terms = gather_terms(edit.instruction, edit.context or edit.find)
        # Research the reviewer's proposed word and the word they marked, so a
        # "should this be 'lightning'?" arrives with how often the book spells it
        # each way — the evidence that turns the guess into a count.
        for extra in (proposal, (edit.find or "").strip(" ,.;:!?\"'“”‘’")):
            if extra and extra not in terms:
                terms.append(extra)
        evidence = term_evidence(stories, terms)
        if not passage and not evidence:
            continue                   # nothing to reason over that it lacked
        try:
            result = provider.complete_structured(
                model=model, system=_SYSTEM,
                user=_brief(edit, passage, evidence, proposal),
                schema=schema, schema_name="escalated_query",
                max_tokens=max_tokens)
        except Exception:              # noqa: BLE001 - a failed read must not lose the flag
            log.warning("Last-tier call failed for %s; the query stays for a "
                        "human", edit.id, exc_info=True)
            continue
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            log.warning("Last tier returned no usable answer for %s (%s); the "
                        "query stays for a human", edit.id, result.stop_reason)
            continue
        verdict = result.parsed
        answer = verdict.get("verdict")
        made = (_resolved_edit(out[at[edit.id]], verdict, passage)
                if answer == "resolve" else None)
        if made is not None:
            out[at[edit.id]] = made
            resolved += 1
            continue
        # A period/capitalization mark the model read as correct as set — a
        # deliberate open line, an intended lowercase. Only for those marks: "leave"
        # on any other query would be the tier answering a person's question with
        # silence, so it falls through to a flag. This clears the mark and keeps
        # the reasoning, which is the whole of "decide it, but record why".
        if answer == "leave" and is_period_cap_query(edit.instruction):
            out[at[edit.id]] = _left_edit(out[at[edit.id]], verdict)
            left += 1
            continue
        # Either it recommended, or a resolution failed a guard — both leave the
        # query flagged, and both are worth the note it wrote.
        note = " ".join((verdict.get("note") or "").split())
        if note:
            out[at[edit.id]] = _replace(out[at[edit.id]], advice=note)
            recommended += 1
    if progress:
        progress(len(candidates), len(candidates))
    if resolved or recommended or left:
        log.info("Last tier read %d query(ies): resolved %d and confirmed %d as "
                 "set on the book's own evidence, advised on %d that stay a "
                 "person's", len(candidates), resolved, left, recommended)
    # A confirm-as-set is a resolution — the query was settled, the mark cleared —
    # so it counts with the resolved for the caller's ledger and log.
    return out, resolved + left, recommended
