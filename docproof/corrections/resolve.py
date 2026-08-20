"""Resolving a flagged correction from the review screen, without InDesign.

A run refuses rather than guesses, so every flag it raises is a correction a
person still owns. Most of them die the same way: the engine knew *what* to
write and could not prove *where* — the text appears six times, the mark cites a
page the match is not on, a gate held an edit back. For those, the choice a
person has to make is a choice between a handful of concrete places, and a
handful of concrete places is a list a screen can show. So the run now leaves a
`queue` in its report: one item per unresolved flag, each carrying the exact
before/after of every place its change could land. A designer clicks the right
one and the corrected IDML is edited in place — the same deterministic write the
run itself would have made, had it been allowed to choose.

The other affordance is a typed answer. A flag the options cannot cover — a
query, a note no edit was extracted for — arrives with a box: the designer says
what to do in plain words ("use the em dash", "change it on the second one",
"delete the whole sentence"), a model turns that answer into an exact edit
against the book's own text, and the edit applies through the same anchoring
machinery as everything else. The designer decides; the model only transcribes
the decision — and an instruction it cannot carry out faithfully is declined
back to the person, never guessed at.

Every resolution is recorded in the report (`record_resolution`), so the change
log stays the one honest account: the flag, who resolved it, and the line as it
now reads.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .apply import _keep_book_case, all_spans, apply_to_stories
from .idml import Story, read_stories, rewrite_stories
from .model import (ApplyReport, CommentDisposition, DESIGN, DISP_FLAGGED,
                    DISP_NOT_EXTRACTED, Edit, EditOutcome, FORMATS, JUDGMENT,
                    MECHANICAL, NO_CHANGE)
from .secondlook import MIN_FIND
from .textmatch import IndexCache

log = logging.getLogger("docproof.corrections.resolve")

# How many places a change could land before the list stops being a choice and
# becomes a search. A find with more matches than this gets no options at all —
# a designer picking comma #14 of 23 from previews is not choosing, and the
# typed box still covers the item.
MAX_MATCHES = 12
# And how many of them to actually show, nearest-the-cited-page first.
MAX_OPTIONS = 6

# What the model call may spend transcribing one typed answer. Generous next to
# the one edit it returns, because the ceiling covers reasoning tokens too and a
# truncated structured reply parses as nothing.
MAX_OUTPUT_TOKENS = 8000

# How much of the book to show the adjudicator: the paragraphs the item's
# options sit in, or the ones its anchor text is found in, clipped so a queue
# item can never drag a whole chapter into the prompt.
MAX_CONTEXT_CHARS = 6000


class ResolveError(RuntimeError):
    """A resolution that cannot be carried out — said in a sentence the review
    screen can show as-is. Nothing has been written when this is raised."""


# --- the queue the run leaves behind -------------------------------------------

def build_queue(stories: list[Story], apply_report: ApplyReport | None,
                comments: Sequence[CommentDisposition] = (), scope=None
                ) -> list[dict]:
    """One item per flag a person still owns, each with every concrete place its
    change could land, computed against `stories` — the *corrected* book, because
    that is the file a resolution will edit.

    Mirrors the report's own accounting: when reviewer comments exist they lead
    (one item per needs-human comment), and flagged edits no comment covers
    follow; a typed list has no comments, so every flagged edit is its own item.
    `scope` is the run's page map, used only to label and order options by the
    page they sit on."""
    if apply_report is None:
        return []
    outcomes = {o.edit.id: o for o in apply_report.outcomes}
    cache = IndexCache()
    queue: list[dict] = []
    covered: set[str] = set()
    n = 0

    def item(comment: CommentDisposition | None, outcome: EditOutcome | None,
             edit_ids: tuple[str, ...]) -> dict:
        nonlocal n
        n += 1
        edit = outcome.edit if outcome is not None else None
        options = (_options_for(edit, stories, scope, cache)
                   if edit is not None else [])
        return {
            "id": f"q{n}",
            "comment_id": comment.id if comment is not None else "",
            "edit_ids": list(edit_ids),
            "page": (comment.page if comment is not None
                     else (edit.page if edit is not None else 0)),
            "instruction": (comment.instruction if comment is not None
                            else (edit.instruction if edit is not None else "")),
            "anchor": comment.anchor if comment is not None else "",
            "status": (comment.disposition if outcome is None
                       and comment is not None else
                       (outcome.status if outcome is not None else "")),
            "why": (comment.detail if comment is not None and comment.detail
                    else (outcome.detail if outcome is not None else "")),
            # What the edit wanted, so the screen can say the change even when
            # no option could be built for it.
            "find": edit.find if edit is not None else "",
            "replace": edit.replace if edit is not None else "",
            "format": edit.format if edit is not None else "",
            "options": options,
            "resolved": None,
        }

    for c in comments:
        if c.disposition not in (DISP_FLAGGED, DISP_NOT_EXTRACTED):
            continue
        covered.update(c.edit_ids)
        queue.append(item(c, _primary_outcome(c.edit_ids, outcomes), c.edit_ids))
    for o in apply_report.flagged:
        if o.edit.id in covered:
            continue
        queue.append(item(None, o, (o.edit.id,)))
    return queue


def _primary_outcome(edit_ids: Sequence[str],
                     outcomes: dict[str, EditOutcome]) -> EditOutcome | None:
    """The outcome to build options from, for a comment that made several edits:
    the first one still needing a person — or the first judgment no-op, which is
    the query shape (`run._apply_status_of` flags those at the comment level)."""
    made = [outcomes[i] for i in edit_ids if i in outcomes]
    for o in made:
        if o.needs_human:
            return o
    for o in made:
        if o.status == NO_CHANGE and o.edit.kind == JUDGMENT:
            return o
    return made[0] if made else None


def _options_for(edit: Edit, stories: list[Story], scope,
                 cache: IndexCache) -> list[dict]:
    """Every place `edit`'s change could land in the corrected book, fully
    materialized — the exact span, the exact replacement, the line before and
    after — so clicking one is choosing, not instructing. Empty when the edit is
    not a text swap (layout and design stay a person's), when its text is
    nowhere, or when it is somewhere too many times to choose from a list."""
    if (not edit.find or edit.is_layout or edit.kind == DESIGN
            or (edit.is_format and edit.find == edit.replace)):
        return []
    matches: list[tuple[Story, object, int, int]] = []
    for s in stories:
        for p in s.paragraphs:
            for start, end in all_spans(p.text, edit.find, cache=cache):
                matches.append((s, p, start, end))
                if len(matches) > MAX_MATCHES:
                    return []
    cited = edit.page
    made: list[dict] = []
    for s, p, start, end in matches:
        found = p.text[start:end]
        replacement = _keep_book_case(found, edit.find, edit.replace)
        if replacement == found:
            continue                      # this copy already reads as asked
        page = scope.page_of(s.story_id, p.index) if scope is not None else 0
        made.append({
            "id": "",                     # numbered after the sort below
            "edit_id": edit.id,
            "story_id": s.story_id,
            "paragraph": p.index,
            "start": start,
            "end": end,
            "found": found,
            "replacement": replacement,
            "before": p.text,
            "after": p.text[:start] + replacement + p.text[end:],
            "page": page,
        })
    # The copy on the cited page first, then reading order — so the likeliest
    # answer is the first thing the designer sees.
    if cited:
        made.sort(key=lambda o: (0 if o["page"] == cited else 1,))
    made = made[:MAX_OPTIONS]
    for i, o in enumerate(made, 1):
        o["id"] = f"{edit.id}-o{i}"
    return made


# --- applying a clicked option -------------------------------------------------

def apply_option(corrected: str | Path, option: dict) -> dict:
    """Write one chosen option into the corrected IDML, in place.

    The span is re-validated against the file as it is *now* — an earlier
    resolution may have edited the same paragraph — and relocated within its
    paragraph when it has merely shifted. A span that is gone, or no longer
    unique, is refused with a sentence rather than guessed at. Returns the
    resolution record: where it landed and the line before and after."""
    corrected = Path(corrected)
    stories = read_stories(corrected)
    story = next((s for s in stories if s.story_id == option["story_id"]), None)
    if story is None:
        raise ResolveError("the corrected file no longer has this story — "
                           "re-run the corrections and try again")
    index = int(option["paragraph"])
    if not 0 <= index < len(story.paragraphs):
        raise ResolveError("the corrected file has changed since this option "
                           "was written — reload the report and try again")
    para = story.paragraphs[index]
    start, end = int(option["start"]), int(option["end"])
    found = option["found"]
    if para.text[start:end] != found:
        spans = [sp for sp in all_spans(para.text, found)
                 if para.text[sp[0]:sp[1]] == found]
        if len(spans) != 1:
            raise ResolveError(
                "this line has changed since the report was written and the "
                "text to change is no longer where it was — reload the report "
                "and try again")
        start, end = spans[0]
    before = para.text
    para.replace(start, end, option["replacement"])
    removed = False
    if not para.text.strip():
        # The option deleted everything the line held; a paragraph with no text
        # sets as a blank line, which is not what removing it meant. Mirrors
        # `apply._remove_emptied`; when the story's shape refuses, the blank
        # line stays and the record says so.
        removed = story.delete_paragraph(index)
    after = "" if removed else story.paragraphs[index].text
    _rewrite_in_place(corrected, {story.story_id: story.serialize()})
    return {"story_id": story.story_id, "paragraph": index,
            "before": before, "after": after, "removed_line": removed}


def _rewrite_in_place(corrected: Path, changed: dict[str, bytes]) -> None:
    """Swap the changed stories into the IDML the designer downloads, atomically:
    the new package lands under a temp name beside it and replaces it in one
    step, so a crash mid-write can never leave a half-written deliverable."""
    fd, tmp_name = tempfile.mkstemp(suffix=".idml", dir=str(corrected.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        rewrite_stories(corrected, tmp, changed)
        os.replace(tmp, corrected)
    finally:
        tmp.unlink(missing_ok=True)


# --- applying a typed answer ---------------------------------------------------

class _Answer(BaseModel):
    decision: Literal["apply", "decline"]
    find: str = ""
    replace: str = ""
    context: str = ""
    # "italic" / "roman" (or another FORMATS key) when the designer's answer is
    # about how the text is set, not what it says.
    format: str = ""
    note: str = ""


_ADJUDICATE_SYSTEM = """\
You are a typesetter's exacting assistant, carrying out a decision a designer \
has already made. A correction on a book proof could not be applied \
automatically and was flagged; the designer has now looked at it and typed \
what to do. Their answer is the decision — your only job is to transcribe it \
into one exact edit against the book's own text, given below.

Produce the edit exactly as the book must carry it:
- find: the exact text to change, copied character for character from the \
BOOK TEXT given — never from the reviewer's note or the designer's answer, \
which are typed and carry straight quotes where the book sets curly ones. \
Only the words that change plus the least surround needed to locate them; \
never a bare punctuation mark.
- replace: that text with the designer's decision carried out, set in the \
book's own marks (curly quotes, the em dash —, the ellipsis …).
- context: a longer verbatim run (the sentence or line) containing the find \
exactly once, whenever the find could appear more than once in the book.
- format: "italic" or "roman" when the answer is about how the text is set; \
the find/replace then carry the words to style (identical when no words \
change). Leave "" otherwise.
- note: a short clause naming what you did ("used the em dash", "changed the \
second copy, as asked").

Decline — decision "decline", with the reason in note — ONLY when the answer \
cannot be carried out as a text edit: it asks for page layout (breaks, \
spacing, where a line falls), it names text that is not in the book text \
given, or it does not actually say what to do ("look at this again"). \
An answer that says to leave the text alone or handle it in InDesign is also \
a decline — say so in the note. Never substitute your own editorial judgment \
for the designer's: carry out what was decided, or decline with the reason."""


def adjudicate_instruction(item: dict, typed: str, provider: Provider, *,
                           model: str, usage: Usage,
                           stories: list[Story]) -> tuple[Edit | None, str]:
    """One typed answer, transcribed into the exact edit it decides.

    Returns `(edit, note)` when the model committed, `(None, reason)` when it
    declined — and a decline is an answer for the designer, not an error. The
    edit still has to anchor and apply like any other; nothing is written
    here."""
    user = _adjudicate_prompt(item, typed, stories)
    try:
        result = provider.complete_structured(
            model=model, system=_ADJUDICATE_SYSTEM, user=user,
            schema=strict_json_schema(_Answer), schema_name="resolve_flag",
            max_tokens=MAX_OUTPUT_TOKENS)
    except Exception as e:             # noqa: BLE001 - surfaced to the screen
        log.warning("Resolve adjudication call failed", exc_info=True)
        raise ResolveError(f"the model could not be reached: {e}")
    usage.add(result.usage, model=model)
    if result.stop_reason != "ok" or result.parsed is None:
        raise ResolveError("the model returned no usable answer — try again, "
                           "or say it differently")
    a = _Answer.model_validate(result.parsed)
    note = " ".join((a.note or "").split())
    if a.decision != "apply":
        return None, (note or "the model could not carry that out as a text "
                              "edit")
    find = (a.find or "").strip("\n")
    fmt = (a.format or "").strip().lower()
    if fmt and fmt not in FORMATS:
        fmt = ""
    if len(find.strip()) < MIN_FIND:
        return None, ("the model did not quote enough of the book's text to "
                      "anchor the change — try naming the exact words")
    if a.replace == find and not fmt:
        return None, ("the model read that as no change at all — if the text "
                      "is already right, it can be left as it is")
    instruction = typed.strip() + (f" — designer resolution: {note}" if note
                                   else " — designer resolution")
    edit = Edit(id=f"{item.get('id', 'q')}-designer", find=find,
                replace=(a.replace if not fmt or a.replace else find),
                context=(a.context or "").strip("\n"), kind=MECHANICAL,
                format=fmt, instruction=instruction)
    return edit, note


def _adjudicate_prompt(item: dict, typed: str, stories: list[Story]) -> str:
    lines = ["THE FLAGGED CORRECTION:"]
    if item.get("page"):
        lines.append(f"- marked on page {item['page']} of the proof")
    if item.get("instruction"):
        lines.append(f"- the reviewer's note: “{item['instruction']}”")
    if item.get("anchor"):
        lines.append(f"- marked on the text: “{item['anchor']}”")
    if item.get("find") and item.get("find") != item.get("replace"):
        lines.append(f"- the change as extracted: “{item['find']}” → "
                     f"“{item['replace']}”")
    if item.get("why"):
        lines.append(f"- why it was flagged: {item['why']}")
    lines += ["", "THE DESIGNER'S ANSWER — the decision to carry out:",
              f"“{typed.strip()}”", ""]
    passages = _context_passages(item, stories)
    if passages:
        lines.append("THE BOOK'S OWN TEXT — copy every find and context from "
                     "it, character for character:")
        lines.append("")
        lines.extend(passages)
    else:
        lines.append("(No passage of the book could be located for this flag; "
                     "decline unless the designer's answer itself quotes the "
                     "exact book text to change.)")
    return "\n".join(lines)


def _context_passages(item: dict, stories: list[Story]) -> list[str]:
    """The paragraphs the answer is about: the ones the item's options sit in,
    or failing those the ones its anchor (then its find) is found in. Bounded,
    deduplicated, in reading order."""
    seen: set[tuple[str, int]] = set()
    out: list[str] = []
    total = 0

    def add(story_id: str, index: int, text: str) -> None:
        nonlocal total
        key = (story_id, index)
        if key in seen or not text.strip() or total >= MAX_CONTEXT_CHARS:
            return
        seen.add(key)
        clipped = text[:MAX_CONTEXT_CHARS - total]
        out.append(f"[story {story_id}, paragraph {index}]")
        out.append(clipped)
        out.append("")
        total += len(clipped)

    for o in item.get("options") or []:
        add(o["story_id"], o["paragraph"], o.get("before") or "")
    if not out:
        cache = IndexCache()
        for probe in (item.get("anchor") or "", item.get("find") or ""):
            if len(probe.strip()) < MIN_FIND:
                continue
            for s in stories:
                for p in s.paragraphs:
                    if all_spans(p.text, probe, cache=cache,
                                 partial_words=True):
                        add(s.story_id, p.index, p.text)
            if out:
                break
    return out


def apply_edit_to_corrected(corrected: str | Path, edit: Edit) -> dict:
    """Apply one adjudicated edit to the corrected IDML through the same
    anchoring machinery a run uses, in place. An edit that does not land — the
    text is nowhere, or somewhere twice — is refused with the same sentence a
    run would have flagged it with, and nothing is written."""
    corrected = Path(corrected)
    stories = read_stories(corrected)
    snapshot = {s.story_id: [p.text for p in s.paragraphs] for s in stories}
    outcomes, changed = apply_to_stories(stories, [edit])
    mine = next((o for o in outcomes if o.edit.id == edit.id), None)
    if mine is None or not mine.applied:
        why = (mine.detail or mine.status) if mine is not None else "unknown"
        raise ResolveError(f"the change could not be applied: {why}")
    removed = any(o.applied and o.edit.id.endswith("-para") for o in outcomes)
    by_id = {s.story_id: s for s in stories}
    before = ""
    lines = snapshot.get(mine.story_id) or []
    if 0 <= mine.paragraph < len(lines):
        before = lines[mine.paragraph]
    story = by_id.get(mine.story_id)
    after = ""
    if not removed and story is not None \
            and 0 <= mine.paragraph < len(story.paragraphs):
        after = story.paragraphs[mine.paragraph].text
    _rewrite_in_place(corrected,
                      {sid: by_id[sid].serialize() for sid in changed})
    return {"story_id": mine.story_id, "paragraph": mine.paragraph,
            "before": before, "after": after, "removed_line": removed,
            "format": edit.format}


# --- keeping the report honest -------------------------------------------------

def queue_counts(payload: dict) -> dict:
    """The card's counters, read off the report as it now stands: edits applied
    (the run's plus every resolution), flagged edits still unresolved, and
    reviewer comments still a person's. The same numbers the run computed,
    re-derived so a resolution moves them."""
    ap = payload.get("apply") or {}
    flagged = [o for o in (ap.get("flagged") or []) if not o.get("resolved")]
    com = payload.get("comments") or {}
    unresolved = [c for c in (com.get("items") or [])
                  if c["disposition"] in (DISP_FLAGGED, DISP_NOT_EXTRACTED)
                  and not c.get("resolved")]
    return {"applied": int(ap.get("applied") or 0), "flags": len(flagged),
            "unresolved": len(unresolved)}


def record_resolution(json_path: str | Path, item_id: str,
                      resolution: dict) -> dict:
    """Fold one resolution into `corrections.json` (and re-render the notes .md
    beside it), so the report the designer reads and the file they download
    never disagree. Returns the updated payload; raises ResolveError for an
    unknown or already-resolved item — checked here, under the caller's lock,
    so two clicks cannot both write."""
    json_path = Path(json_path)
    payload = json.loads(json_path.read_text("utf-8"))
    queue = payload.get("queue") or []
    item = next((q for q in queue if q.get("id") == item_id), None)
    if item is None:
        raise ResolveError("this flag is not in the report — reload it and "
                           "try again")
    if item.get("resolved"):
        raise ResolveError("this flag was already resolved")
    resolution = dict(resolution)
    resolution["at"] = datetime.now(timezone.utc).isoformat()
    item["resolved"] = resolution

    ap = payload.get("apply")
    if ap is not None:
        ap["applied"] = int(ap.get("applied") or 0) + 1
        # One resolution applies one change, so it retires exactly one flagged
        # edit — the one the clicked option belonged to, or the item's first
        # still-flagged edit for a typed answer. A comment that raised several
        # flags keeps the others standing; the ledger stays a sum that checks.
        wanted = ([resolution["edit_id"]] if resolution.get("edit_id")
                  else list(item.get("edit_ids") or []))
        for o in ap.get("flagged") or []:
            if o["id"] in wanted and not o.get("resolved"):
                o["resolved"] = True
                break
    com = payload.get("comments") or {}
    if item.get("comment_id"):
        for c in com.get("items") or []:
            if c["id"] == item["comment_id"]:
                c["resolved"] = True
        com["unresolved"] = sum(
            1 for c in com.get("items") or []
            if c["disposition"] in (DISP_FLAGGED, DISP_NOT_EXTRACTED)
            and not c.get("resolved"))
    note = ("resolved in review by the designer"
            + (f" — “{resolution['text']}”" if resolution.get("text") else ""))
    payload.setdefault("changes", []).append({
        "story_id": resolution.get("story_id", ""),
        "paragraph": resolution.get("paragraph", -1),
        "before": resolution.get("before", ""),
        "after": resolution.get("after", ""),
        "edit_ids": list(item.get("edit_ids") or [item_id]),
        "instruction": " — ".join(x for x in (item.get("instruction"), note)
                                  if x),
        "formatting": resolution.get("format", ""),
        "resolved_in_review": True,
    })
    payload.setdefault("resolutions", []).append({"item_id": item_id,
                                                  **resolution})
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path = json_path.parent / "corrections_notes.md"
    try:
        from .report import _markdown
        md_path.write_text(_markdown(payload), encoding="utf-8")
    except Exception:                  # noqa: BLE001 - the JSON is the record
        log.warning("Could not re-render %s after a resolution", md_path,
                    exc_info=True)
    return payload
