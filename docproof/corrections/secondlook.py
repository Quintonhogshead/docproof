"""An optional second read over the corrections the deterministic run must flag.

Three classes of flag come out of a run, and each has a repair a stronger model
can make without ever being allowed to guess:

* **Queries.** The extractor is deliberately timid: a note that offers
  alternatives ("replace the comma with an em dash or a semicolon") or hangs on
  a condition ("if this is spoken aloud, make the quotes double") is emitted as
  a judgment — a query for a person — because committing to one reading is a
  decision, and the first pass is not allowed decisions. But half of those
  queries are not questions. A reviewer who writes "em dash or semicolon" has
  already ordered the change and delegated one detail; a conditional whose
  condition the page itself settles is an instruction wearing an if.
  `settle_queries` puts exactly that set to the model and asks it to commit or
  decline; a real question is declined and stays a human's.

* **Lost anchors.** An edit whose `find` is nowhere in the book, or repeated
  with nothing to choose between the copies, is usually a *quoting* failure,
  not a correction failure: the anchor carries a PDF artifact — a stray space,
  a line-end hyphen, a straight quote — that none of the matching tiers could
  bridge. `reanchor_edits` shows the model the book's own text for the page the
  mark sat on and asks it to re-quote the same correction from it, character
  for character. The correction itself is not up for revision.

* **Collisions.** Two corrections aimed at overlapping spans of one line — the
  second is refused so it cannot garble what the first wrote. But the reviewer
  meant both, and both against the *original* line; `merge_overlaps` puts the
  colliding pair side by side and asks for one edit that carries every change
  at once, which then lands in a single write.

The safety argument is the extraction stage's, unchanged: the model only
proposes. Everything it settles, re-anchors or merges still has to anchor
against the real book in `apply` (a find that is not there is flagged, never
guessed), the opt-in sanity gate reads its output like any other edit's, and
the change log shows the note it wrote beside the reviewer's own, so every
choice is visible, not silent.

Opt-in, like the sanity gate: the default corrections run stays deterministic
and free. A call that fails repairs nothing and the flags stand — a broken
second read must not lose the flags the first one raised.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Callable, Literal, Sequence

from pydantic import BaseModel

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .apply import apply_to_stories
from .idml import Story
from .model import (AMBIGUOUS, DESIGN, Edit, JUDGMENT, MECHANICAL, NOT_FOUND,
                    OVERLAPS)

log = logging.getLogger("docproof.corrections.secondlook")

MAX_OUTPUT_TOKENS = 8000
# Repair a bounded batch at a time, for the same reason the extractor reads one:
# a structured reply that overruns the output ceiling parses as nothing, and here
# that would silently drop every call the model just made.
BATCH_SIZE = 20
# A find this short is not an anchor — the extraction prompt bans a bare "," for
# the same reason — so an answer that arrives with one is refused here rather
# than sent on to fail loudly in apply.
MIN_FIND = 3


class _Choice(BaseModel):
    id: str
    decision: Literal["settled", "needs_human"]
    find: str = ""
    replace: str = ""
    context: str = ""
    note: str = ""


class _Choices(BaseModel):
    choices: list[_Choice]


_SETTLE_SYSTEM = """\
You are a senior copy editor making the calls a reviewer left open. Each item \
is a note a reviewer marked on a proof of a book — one a first read would not \
turn into a concrete edit — together with the line it was marked on and, where \
given, the book's own text for that page.

Settle a note ONLY when the reviewer has already ordered the change and merely \
left a detail to you:
- The note offers alternatives ("replace the comma with an em dash or a \
semicolon", "period (or semicolon)"). The change itself is decided; make it, \
picking whichever alternative reads best in the sentence. When both read \
equally well, take the one the reviewer named first.
- The note is conditional and the text in front of you settles the condition \
("if this is spoken aloud, make the quotes double" beside a tag that says \
"I said"). Carry out the branch the text selects.
- The note states a change plainly but loosely, and the line leaves exactly one \
way to read it.

NEVER settle:
- A genuine question ("Should this be deleted?", "Is there a typo here?", \
"Remove blank line break?") — the reviewer is asking, not delegating. Asking \
which of two texts is right is a question; only a note where the change is \
ordered and the alternatives are interchangeable renderings is delegated.
- A note that needs the author's intent, pages you were not given, or a \
preference the text cannot settle.
- A note about how the page composed (a bad break, a widow, spacing) — that is \
InDesign's, not a text edit.
When in doubt, decline: a wrong guess applied to the book is worse than a flag.

For a settled note, produce the edit exactly as the book must carry it:
- find: the exact text to change, copied character for character from the \
book's own text for that page (or from the marked line when no page text is \
given). Only the words that change plus the least surround needed to locate \
them — never a bare punctuation mark; quote a few words around it.
- replace: the corrected text, set in the book's own marks (curly quotes, the \
em dash —, the ellipsis …).
- context: a longer verbatim run around the find (the sentence or line), \
whenever the find could appear more than once on the page.
- note: a short clause naming the call you made ("chose the em dash", "the tag \
says it is spoken, so double quotes").

Return one entry per item id, decision "settled" or "needs_human" — never \
invent an item and never drop one."""


_REANCHOR_SYSTEM = """\
You are a copy editor's second pair of eyes over corrections that could not be \
located in the book. Each item is one correction — the reviewer's note, the \
exact change it asked for, and why it could not land — together with the \
book's own text for the page it was marked on.

The correction itself is already decided; never revisit it. Your only job is \
the anchor. The quoted find usually differs from the book by an artifact of \
how the proof was read — a stray space inside a word, a hyphen at a line end, \
a straight quote where the book sets a curly one — or it is a common run the \
mark repeated and nothing chose between the copies. Find the passage the \
reviewer meant in the book text given and re-quote it:

- find: copied VERBATIM from the book text — same words, punctuation, \
capitalization. Only the words the change touches plus the least surround \
needed to locate them; never a bare punctuation mark.
- replace: the SAME correction the item asked for, applied to your re-quoted \
find. Change nothing beyond what the original asked. (For an item that styles \
or re-sets its text rather than rewriting it — its original find equals its \
replace — set replace identical to your find.)
- context: when your find appears more than once on the page, also copy a \
longer verbatim run (the sentence or line) that contains it exactly once, so \
the right copy is chosen.
- note: a short clause on what you re-anchored ("the book has 'LimeWire', \
unbroken").

Return decision "settled" when you found the passage the correction \
describes; "needs_human" when the page's text does not contain it — never \
anchor to a different passage than the reviewer marked, and never invent a \
change the item did not ask for."""


_MERGE_SYSTEM = """\
You are a copy editor resolving corrections that collided: two (or more) \
marks on the same few words of a line, each asking for its own change. \
Applied separately the second would overwrite the first, so the engine \
refused it — but the reviewer meant all of them, each against the original \
line. Each item gives every colliding correction (its note and the exact \
change it asked for) and the book's own text for the page.

Produce ONE edit that carries every one of the corrections at once:
- find: the one run of the original book text that covers everything the \
corrections touch, copied VERBATIM from the book text given.
- replace: that run with ALL of the corrections applied — and nothing else \
changed.
- context: a longer verbatim run containing the find, when the find could \
appear more than once on the page.
- note: a short clause on how they combined ("period, then capital, then the \
comma after").

Return decision "settled" when the corrections compose cleanly; \
"needs_human" when they contradict each other (two different rewrites of the \
same words) — never pick a winner between contradicting corrections."""


# --- the dry run --------------------------------------------------------------

def probe(stories: list[Story], edits: Sequence[Edit], scope=None
          ) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """What a real apply of `edits` would flag, found by running one on
    throwaway stories. Returns `(lost, collisions)`: edit ids that would fail to
    anchor (NOT_FOUND / AMBIGUOUS) mapped to why, and edit ids that would be
    refused for overlapping an applied edit, mapped to the id(s) they hit.

    `stories` is mutated — pass a fresh `read_stories`, never the list another
    stage is still using."""
    outcomes, _ = apply_to_stories(stories, list(edits), scope=scope)
    lost: dict[str, str] = {}
    collisions: dict[str, tuple[str, ...]] = {}
    for o in outcomes:
        if o.status in (NOT_FOUND, AMBIGUOUS):
            lost[o.edit.id] = o.detail or o.status
        elif o.status == OVERLAPS and o.collides_with:
            collisions[o.edit.id] = o.collides_with
    return lost, collisions


# --- shared machinery ---------------------------------------------------------

def _page_blocks(pages: Sequence[int], book_pages: dict[int, str]) -> list[str]:
    lines = []
    wanted = sorted({p for p in pages if (book_pages.get(p) or "").strip()})
    if wanted:
        lines += ["", "THE BOOK'S OWN TEXT for those pages — copy every find "
                      "and context from it, character for character:", ""]
        for p in wanted:
            lines.append(f"[book text, page {p}]")
            lines.append(book_pages[p])
            lines.append("")
    return lines


def _ask(provider: Provider, *, model: str, usage: Usage, system: str,
         user: str, max_tokens: int) -> list[dict] | None:
    """One structured call, or None on any failure — the caller then leaves its
    batch exactly as it was, which is the direction this pass may fail in."""
    try:
        result = provider.complete_structured(
            model=model, system=system, user=user,
            schema=strict_json_schema(_Choices), schema_name="second_look",
            max_tokens=max_tokens)
    except Exception:              # noqa: BLE001 - a failed read must not lose the flags
        log.warning("Second look call failed; leaving this batch's flags for "
                    "a human", exc_info=True)
        return None
    usage.add(result.usage, model=model)
    if result.stop_reason != "ok" or result.parsed is None:
        log.warning("Second look returned no usable answer (%s); leaving this "
                    "batch's flags for a human", result.stop_reason)
        return None
    return result.parsed.get("choices", [])


def _settled_only(choices: list[dict], wanted: set[str]):
    """The (id, choice) pairs that settled a known item — one answer per item,
    the first."""
    for c in choices:
        cid = c.get("id")
        if c.get("decision") == "settled" and cid in wanted:
            wanted.discard(cid)
            yield cid, c


def _annotated(instruction: str, what: str, note: str) -> str:
    note = " ".join((note or "").split())
    tail = f" — second look: {what}: {note}" if note else f" — second look: {what}"
    return (instruction or "").rstrip() + tail


# --- the three repairs --------------------------------------------------------

def is_query(e: Edit) -> bool:
    """Whether an edit is the kind of flag `settle_queries` exists to settle: a
    note the extractor read but would not commit to.

    Any judgment carrying a note qualifies, whether or not it arrived with a
    proposed rewrite. A judgment never applies on its own (apply holds every one
    for a person), so a proposal it carries is a candidate answer, not a settled
    edit — "em dash or semicolon" and "Should this be could?" both reach the model
    here, which settles the delegated ones and declines the real questions. Format
    and paragraph requests are already concrete, so neither is re-asked."""
    return (e.kind == JUDGMENT and not e.format and not e.paragraph
            and bool((e.instruction or "").strip()))


def _settled_edit(original: Edit, choice: dict) -> Edit | None:
    """The concrete edit one settled query makes, or None when the answer does
    not survive the guards — too short a find to anchor, or a change that
    changes nothing. The reviewer's note is kept verbatim and the model's call
    appended after it, so the change log shows who decided what."""
    find = (choice.get("find") or "").strip("\n")
    rep = choice.get("replace") or ""
    if len(find.strip()) < MIN_FIND or rep == find:
        return None
    return replace(original, find=find, replace=rep,
                   context=(choice.get("context") or "").strip("\n"),
                   kind=MECHANICAL,
                   instruction=_annotated(original.instruction, "settled",
                                          choice.get("note") or ""))


def settle_queries(edits: Sequence[Edit], provider: Provider, *, model: str,
                   usage: Usage, book_pages: dict[int, str] | None = None,
                   max_tokens: int = MAX_OUTPUT_TOKENS,
                   progress: Callable[[int, int], None] | None = None
                   ) -> tuple[list[Edit], int]:
    """The edit list with every query the model settled made concrete, in place
    and in order, plus how many it settled.

    Only queries are put to the model (see `is_query`); everything else rides
    through untouched. `usage` accrues the spend. A batch whose call fails is
    logged and left as it was — the queries stand for a human, exactly as if
    the pass had not run.

    `progress(done, total)`, when given, is called a batch at a time so the
    caller can show the pass moving through the queries."""
    out = list(edits)
    at = {e.id: i for i, e in enumerate(out)}
    candidates = [e for e in out if is_query(e)]
    if not candidates:
        return out, 0
    pages = book_pages or {}
    settled = 0
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i + BATCH_SIZE]
        if progress:
            progress(i, len(candidates))
        lines = ["The reviewer's open notes:", ""]
        for e in batch:
            where = f" (page {e.page})" if e.page else ""
            lines.append(f"- id {e.id}{where}: the reviewer wrote: "
                         f"\"{e.instruction}\"")
            anchor = e.context or e.find
            if anchor:
                lines.append(f"    on the line: \"{anchor}\"")
        lines += _page_blocks([e.page for e in batch], pages)
        choices = _ask(provider, model=model, usage=usage,
                       system=_SETTLE_SYSTEM, user="\n".join(lines),
                       max_tokens=max_tokens)
        if choices is None:
            continue
        for cid, c in _settled_only(choices, {e.id for e in batch}):
            made = _settled_edit(out[at[cid]], c)
            if made is not None:
                out[at[cid]] = made
                settled += 1
    if progress:
        progress(len(candidates), len(candidates))
    if settled:
        log.info("Second look settled %d of %d query(ies); the rest stay for "
                 "a human", settled, len(candidates))
    return out, settled


def _asked_change(e: Edit) -> str:
    """One line saying what an edit asks for, for the re-anchor and merge
    prompts."""
    if e.format:
        return f"set \"{e.find}\" {e.format}"
    if e.paragraph:
        return f"{e.paragraph} at \"{e.find}\""
    if e.replace == "":
        return f"delete \"{e.find}\""
    return f"change \"{e.find}\" to \"{e.replace}\""


def _reanchored_edit(original: Edit, choice: dict, page_text: str
                     ) -> Edit | None:
    """The same correction on a re-quoted anchor, or None when the answer does
    not survive the guards. The one deterministic check that matters is that
    the new find really is in the book text the model was told to copy from —
    an answer that is not was recalled, not quoted, which is the exact failure
    this pass exists to repair, not to repeat."""
    find = (choice.get("find") or "").strip("\n")
    if len(find.strip()) < MIN_FIND or find not in page_text:
        return None
    if original.format or original.paragraph:
        rep = find                     # these style or re-set; the text stands
    else:
        rep = choice.get("replace") or ""
        if rep == find:
            return None                # the correction itself went missing
    context = (choice.get("context") or "").strip("\n")
    if context and find not in context:
        context = ""
    return replace(original, find=find, replace=rep, context=context,
                   occurrence=0,       # the old ordinal counted the old find
                   instruction=_annotated(original.instruction, "re-anchored",
                                          choice.get("note") or ""))


def reanchor_edits(edits: Sequence[Edit], lost: dict[str, str],
                   provider: Provider, *, model: str, usage: Usage,
                   book_pages: dict[int, str],
                   max_tokens: int = MAX_OUTPUT_TOKENS
                   ) -> tuple[list[Edit], int]:
    """The edit list with every lost anchor the model could re-quote from the
    book's own page text repaired in place, plus how many were repaired.

    `lost` maps edit id → why it failed to anchor, from `probe`. Only an edit
    whose page the map placed is asked about — the book text for that page is
    both what the model quotes from and the guard its answer is checked
    against. Everything else, and every answer that fails a guard, is left
    exactly as it was: flagged."""
    out = list(edits)
    at = {e.id: i for i, e in enumerate(out)}
    candidates = [e for e in out
                  if e.id in lost and e.kind != DESIGN and e.page
                  and (book_pages.get(e.page) or "").strip()]
    if not candidates:
        return out, 0
    fixed = 0
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i + BATCH_SIZE]
        lines = ["Corrections that could not be located:", ""]
        for e in batch:
            lines.append(f"- id {e.id} (page {e.page}): {_asked_change(e)}")
            if e.instruction:
                lines.append(f"    the reviewer's note: \"{e.instruction}\"")
            if e.context:
                lines.append(f"    marked context: \"{e.context}\"")
            lines.append(f"    why it failed: {lost[e.id]}")
        lines += _page_blocks([e.page for e in batch], book_pages)
        choices = _ask(provider, model=model, usage=usage,
                       system=_REANCHOR_SYSTEM, user="\n".join(lines),
                       max_tokens=max_tokens)
        if choices is None:
            continue
        for cid, c in _settled_only(choices, {e.id for e in batch}):
            original = out[at[cid]]
            made = _reanchored_edit(original, c, book_pages[original.page])
            if made is not None:
                out[at[cid]] = made
                fixed += 1
    if fixed:
        log.info("Second look re-anchored %d of %d lost edit(s); the rest stay "
                 "for a human", fixed, len(candidates))
    return out, fixed


def _mergeable(e: Edit | None) -> bool:
    """Whether an edit can take part in a merge: a plain, concrete text rewrite. A
    format or paragraph request cannot be expressed inside a combined find→replace,
    a structural edit moves text a span cannot describe, a design note edits
    nothing, and a judgment is a person's call that must not be folded into a merge
    and thereby applied — a collision involving any of them stays a human's."""
    return (e is not None and e.kind != DESIGN and e.kind != JUDGMENT
            and not e.format and not e.paragraph)


def merge_overlaps(edits: Sequence[Edit],
                   collisions: dict[str, tuple[str, ...]],
                   provider: Provider, *, model: str, usage: Usage,
                   book_pages: dict[int, str],
                   max_tokens: int = MAX_OUTPUT_TOKENS
                   ) -> tuple[list[Edit], int, int]:
    """The edit list with each set of colliding corrections replaced by the one
    combined edit the model composed, how many sets were merged, and how many edits
    that removed from the list — so the run can still account for every id it parsed
    even after several became one.

    `collisions` maps a refused edit's id → the applied edit id(s) it hit, from
    `probe`. Each group — the refused edit and everything it collided with,
    with groups sharing a member folded together — is put to the model as one
    item. A settled answer replaces the group's first member (which keeps its
    id, its page, and the union of the group's comment sources, so every
    reviewer comment still reaches an outcome in the ledger) and the other
    members are dropped from the list. A declined or guarded answer leaves the
    whole group untouched: the same collision, the same flag, a human's."""
    out = list(edits)
    by_id = {e.id: e for e in out}
    # Fold colliding pairs into groups, joining any that share a member — two
    # refused edits that both hit the same applied one are one story, not two.
    groups: list[set[str]] = []
    for eid, hit in collisions.items():
        members = {eid, *hit}
        joined = [g for g in groups if g & members]
        for g in joined:
            members |= g
            groups.remove(g)
        groups.append(members)
    order = {e.id: i for i, e in enumerate(out)}
    workable = []
    for g in groups:
        member_edits = [by_id.get(m) for m in sorted(g, key=lambda m:
                                                     order.get(m, 1 << 30))]
        page = next((e.page for e in member_edits
                     if e is not None and e.page
                     and (book_pages.get(e.page) or "").strip()), 0)
        if page and all(_mergeable(e) for e in member_edits):
            workable.append((member_edits, page))
    if not workable:
        return out, 0, 0
    merged = merged_away = 0
    for i in range(0, len(workable), BATCH_SIZE):
        batch = workable[i:i + BATCH_SIZE]
        lines = ["Corrections that collided on the same words:", ""]
        for members, page in batch:
            first = members[0]
            lines.append(f"- id {first.id} (page {page}): "
                         f"{len(members)} corrections on one span:")
            for e in members:
                note = f"  (note: {e.instruction})" if e.instruction else ""
                lines.append(f"    * {_asked_change(e)}{note}")
        lines += _page_blocks([page for _, page in batch], book_pages)
        wanted = {members[0].id: (members, page) for members, page in batch}
        choices = _ask(provider, model=model, usage=usage,
                       system=_MERGE_SYSTEM, user="\n".join(lines),
                       max_tokens=max_tokens)
        if choices is None:
            continue
        for cid, c in _settled_only(choices, set(wanted)):
            members, page = wanted[cid]
            made = _merged_edit(members, c, book_pages[page])
            if made is not None:
                out[order[cid]] = made
                dropped = {e.id for e in members[1:]}
                out = [e for e in out if e.id not in dropped]
                order = {e.id: j for j, e in enumerate(out)}
                merged += 1
                merged_away += len(dropped)
    if merged:
        log.info("Second look merged %d colliding set(s), combining %d edit(s) "
                 "into others; the rest stay for a human", merged, merged_away)
    return out, merged, merged_away


def _merged_edit(members: list[Edit], choice: dict, page_text: str
                 ) -> Edit | None:
    """One edit carrying a whole colliding group's corrections, or None when
    the answer does not survive the guards. It wears the first member's id and
    the union of the group's comment sources, so the ledger still accounts for
    every reviewer comment the group came from."""
    find = (choice.get("find") or "").strip("\n")
    rep = choice.get("replace") or ""
    if len(find.strip()) < MIN_FIND or find not in page_text or rep == find:
        return None
    context = (choice.get("context") or "").strip("\n")
    if context and find not in context:
        context = ""
    sources: list[str] = []
    for e in members:
        for cid in (e.source or "").split():
            if cid not in sources:
                sources.append(cid)
    notes = "; ".join(e.instruction for e in members if e.instruction)
    return replace(members[0], find=find, replace=rep, context=context,
                   occurrence=0, kind=MECHANICAL, source=" ".join(sources),
                   instruction=_annotated(notes, "merged",
                                          choice.get("note") or ""))
