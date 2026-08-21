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

import difflib

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .apply import _keep_book_case, all_spans, apply_to_stories
from .idml import Story, read_stories, rewrite_stories
from .model import (ApplyReport, CommentDisposition, DESIGN, DISP_FLAGGED,
                    DISP_NOT_EXTRACTED, Edit, EditOutcome, FORMATS, JUDGMENT,
                    MECHANICAL, NO_CHANGE, NO_CHARACTER_STYLE, is_italic_style,
                    style_name)
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
                comments: Sequence[CommentDisposition] = (), scope=None,
                book_pages: dict[int, str] | None = None) -> list[dict]:
    """One item per flag a person still owns, each with every concrete place its
    change could land, computed against `stories` — the *corrected* book, because
    that is the file a resolution will edit.

    Mirrors the report's own accounting: when reviewer comments exist they lead
    (one item per needs-human comment), and flagged edits no comment covers
    follow; a typed list has no comments, so every flagged edit is its own item.
    `scope` is the run's page map, used only to label and order options by the
    page they sit on. `book_pages` is the book's own text per cited page, from
    the run — stamped onto each item so a typed answer's adjudicator can read
    the whole page even when no option could be built."""
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
        page = (comment.page if comment is not None
                else (edit.page if edit is not None else 0))
        return {
            "id": f"q{n}",
            "comment_id": comment.id if comment is not None else "",
            "edit_ids": list(edit_ids),
            "page": page,
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
            # What a model pass already concluded about this flag — the advice
            # the escalate tier wrote for the person who owns it. Kept on the
            # item so the screen can offer carrying it out as one click.
            "advice": edit.advice if edit is not None else "",
            # The book's own words for the cited page, so a typed answer's
            # adjudicator reads the page the reviewer read — not just the
            # paragraphs a candidate happened to land in.
            "page_text": ((book_pages or {}).get(page) or "")[:MAX_CONTEXT_CHARS],
            "options": options,
            # Where the manual editor can open when there is nothing to click:
            # the paragraph the flag was located in, or the one(s) carrying the
            # marked text. Options already carry their own locations.
            "targets": _targets_for(
                outcome, comment.anchor if comment is not None else "",
                options, stories, scope, cache),
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


def _targets_for(outcome: EditOutcome | None, anchor: str, options: list[dict],
                 stories: list[Story], scope, cache: IndexCache) -> list[dict]:
    """The paragraphs the manual editor can open for a flag with nothing to
    click: where the outcome was located (a design note, a placed query), or
    failing that the paragraph(s) carrying the marked text. Deduplicated
    against the options, which already carry their own locations."""
    seen = {(o["story_id"], o["paragraph"]) for o in options}
    by_story = {s.story_id: s for s in stories}
    out: list[dict] = []

    def add(story_id: str, index: int) -> None:
        key = (story_id, index)
        story = by_story.get(story_id)
        if (key in seen or story is None
                or not 0 <= index < len(story.paragraphs)):
            return
        text = story.paragraphs[index].text
        if not text.strip():
            return
        seen.add(key)
        out.append({"story_id": story_id, "paragraph": index, "before": text,
                    "page": (scope.page_of(story_id, index)
                             if scope is not None else 0)})

    if (outcome is not None and outcome.story_id
            and outcome.paragraph >= 0):
        add(outcome.story_id, outcome.paragraph)
    if not out and len((anchor or "").strip()) >= MIN_FIND:
        for s in stories:
            for p in s.paragraphs:
                if len(out) >= 2:
                    return out
                if all_spans(p.text, anchor, cache=cache, partial_words=True):
                    add(s.story_id, p.index)
    return out


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


# --- reading and hand-editing one paragraph ------------------------------------
# The manual mode: the designer opens the line itself, retypes what needs
# retyping, bolds or italicizes a selection, adds or removes the section break
# beside it — and the save writes exactly that into the corrected IDML. The
# engine's job here is fidelity: only the characters that changed are touched
# (so the formatting of everything around them survives), and styling is
# applied per run against what the book actually carries.

# What counts as a section break between paragraphs: a blank line, or an
# ornament line — a short run of non-letter marks ("* * *", "⁂", "~") that
# typesets as a scene divider.
_BREAK_MAX = 12


def _is_break_para(text: str) -> bool:
    s = (text or "").strip()
    return not s or (len(s) <= _BREAK_MAX and not any(c.isalnum() for c in s))


def _char_states(para) -> list[tuple[bool, bool, bool]]:
    """(bold, italic, italic-from-style) for every character of the paragraph,
    read off the character range each Content node sits in. `italic-from-style`
    is kept apart because clearing it means clearing the applied style, not a
    FontStyle override."""
    states: list[tuple[bool, bool, bool]] = []
    for node in para.nodes:
        csr = node.getparent()
        face = ((csr.get("FontStyle") or "") if csr is not None else "").lower()
        applied = ((csr.get("AppliedCharacterStyle") or "")
                   if csr is not None else "")
        style_italic = is_italic_style(applied)
        bold = "bold" in face or "bold" in style_name(applied).lower()
        italic = "italic" in face or "oblique" in face or style_italic
        states.extend([(bold, italic, style_italic)] * len(node.text or ""))
    return states


def paragraph_state(stories: list[Story], story_id: str, index: int, *,
                    expect: str = "") -> dict:
    """One paragraph as the manual editor loads it: its text, its bold/italic
    runs, and whether a section break sits either side of it — so the editor
    can offer removing one that exists and only that.

    `expect` is the text the caller believes the paragraph holds — the report
    recorded it at run time, and a resolution since may have inserted or
    removed a line and renumbered everything after it. When the paragraph at
    `index` no longer reads as expected, the story is searched for the one
    paragraph that does, and *that* is returned (its current index included),
    so the editor never opens on a neighbour by silent off-by-one. Found
    nowhere or twice, it is refused instead of guessed."""
    story = next((s for s in stories if s.story_id == story_id), None)
    if story is None:
        raise ResolveError("the corrected file no longer has this story — "
                           "re-run the corrections and try again")
    if expect and not (0 <= index < len(story.paragraphs)
                       and story.paragraphs[index].text == expect):
        matches = [p.index for p in story.paragraphs if p.text == expect]
        if len(matches) != 1:
            raise ResolveError(
                "this line has changed since the report was written — reload "
                "the report and try again")
        index = matches[0]
    if not 0 <= index < len(story.paragraphs):
        raise ResolveError("the corrected file has changed since the report "
                           "was written — reload it and try again")
    para = story.paragraphs[index]
    runs: list[dict] = []
    for i, (bold, italic, _s) in enumerate(_char_states(para)):
        if runs and runs[-1]["bold"] == bold and runs[-1]["italic"] == italic:
            runs[-1]["end"] = i + 1
        else:
            runs.append({"start": i, "end": i + 1, "bold": bold,
                         "italic": italic})
    prev_text = (story.paragraphs[index - 1].text if index > 0 else None)
    next_text = (story.paragraphs[index + 1].text
                 if index + 1 < len(story.paragraphs) else None)
    return {
        "story_id": story_id, "paragraph": index,
        "text": para.text, "runs": runs,
        "prev_break": prev_text is not None and _is_break_para(prev_text),
        "next_break": next_text is not None and _is_break_para(next_text),
    }


def apply_manual(corrected: str | Path, spec: dict) -> dict:
    """Write one hand-edited paragraph into the corrected IDML, in place.

    `spec` carries what the editor holds: the paragraph's address, the text it
    loaded (`expected`, the staleness guard), the text as edited, the desired
    bold/italic runs over that text, and the section-break asks. Only the
    characters that differ are rewritten — the diff is applied span by span, so
    the formatting of untouched text survives — and styling is changed only
    where the desired state differs from what the book carries. Anything that
    cannot be done cleanly is refused with a sentence; nothing is written on a
    refusal."""
    corrected = Path(corrected)
    stories = read_stories(corrected)
    story = next((s for s in stories
                  if s.story_id == spec.get("story_id")), None)
    if story is None:
        raise ResolveError("the corrected file no longer has this story — "
                           "re-run the corrections and try again")
    index = int(spec.get("paragraph", -1))
    if not 0 <= index < len(story.paragraphs):
        raise ResolveError("the corrected file has changed since the editor "
                           "opened — reload the report and try again")
    para = story.paragraphs[index]
    before = para.text
    if before != (spec.get("expected") or ""):
        raise ResolveError("this line has changed since the editor opened — "
                           "reopen it and try again")
    new_text = spec.get("text")
    if new_text is None:
        raise ResolveError("the edited text is missing")
    insert_after = bool(spec.get("insert_break_after"))
    rm_above = bool(spec.get("remove_break_above"))
    rm_below = bool(spec.get("remove_break_below"))
    if insert_after and rm_below:
        raise ResolveError("adding a break after this paragraph and removing "
                           "the one below it contradict — pick one")

    removed_line = False
    changed = False
    if not new_text.strip():
        # Clearing the whole line means removing it — a paragraph with no text
        # sets as a blank line, which is not what deleting the words meant.
        if insert_after or rm_above or rm_below:
            raise ResolveError("clearing the whole line removes it — the "
                               "breaks around it can't be edited in the same "
                               "save")
        if not story.delete_paragraph(index):
            raise ResolveError("this line could not be removed cleanly — "
                               "delete it in InDesign")
        after = ""
        removed_line = changed = True
    else:
        # The words: only the spans that differ are rewritten, in reverse so
        # each earlier span's offsets still hold.
        matcher = difflib.SequenceMatcher(a=before, b=new_text, autojunk=False)
        ops = [op for op in matcher.get_opcodes() if op[0] != "equal"]
        for _tag, i1, i2, j1, j2 in reversed(ops):
            para.replace(i1, i2, new_text[j1:j2])
        changed = changed or bool(ops)

        # The styling: desired state per character vs what the book now
        # carries, restyled only where they differ — split where the source of
        # an italic differs too, because clearing a styled italic means
        # clearing the style, not writing an override.
        desired = [(False, False)] * len(new_text)
        for r in spec.get("runs") or []:
            lo = max(0, int(r.get("start", 0)))
            hi = min(len(new_text), int(r.get("end", 0)))
            state = (bool(r.get("bold")), bool(r.get("italic")))
            for i in range(lo, hi):
                desired[i] = state
        current = _char_states(para)
        i = 0
        while i < len(new_text):
            want = desired[i]
            have = current[i]
            j = i
            while (j < len(new_text) and desired[j] == want
                   and current[j] == have):
                j += 1
            if want != (have[0], have[1]):
                bold, italic = want
                face = ("Bold Italic" if bold and italic else
                        "Bold" if bold else "Italic" if italic else None)
                attrs: dict = {"FontStyle": face}
                if have[2] and not italic:
                    attrs["AppliedCharacterStyle"] = NO_CHARACTER_STYLE
                if not para.restyle(i, j, attrs):
                    raise ResolveError(
                        "the styling could not be applied here — the text is "
                        "not held by a character range; do this one in "
                        "InDesign")
                changed = True
            i = j
        after = para.text

        # The breaks: the side below first, because removing the line above
        # renumbers this one.
        if rm_below:
            if not (index + 1 < len(story.paragraphs)
                    and _is_break_para(story.paragraphs[index + 1].text)):
                raise ResolveError("there is no blank line or break marker "
                                   "below this paragraph to remove")
            if not story.delete_paragraph(index + 1):
                raise ResolveError("the break below could not be removed "
                                   "cleanly — do it in InDesign")
            changed = True
        if insert_after:
            if not story.insert_paragraph(index, "", after=True):
                raise ResolveError("a section break could not be added here — "
                                   "do it in InDesign")
            changed = True
        if rm_above:
            if not (index > 0
                    and _is_break_para(story.paragraphs[index - 1].text)):
                raise ResolveError("there is no blank line or break marker "
                                   "above this paragraph to remove")
            if not story.delete_paragraph(index - 1):
                raise ResolveError("the break above could not be removed "
                                   "cleanly — do it in InDesign")
            index -= 1
            changed = True

    if not changed:
        raise ResolveError("nothing was changed — edit the line, the styling "
                           "or a break first")
    _rewrite_in_place(corrected, {story.story_id: story.serialize()})
    return {"story_id": story.story_id, "paragraph": index,
            "before": before, "after": after, "removed_line": removed_line,
            "breaks": {"added_after": insert_after,
                       "removed_above": rm_above,
                       "removed_below": rm_below}}


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
                           stories: list[Story],
                           history: Sequence[dict] | None = None
                           ) -> tuple[Edit | None, str]:
    """One typed answer, transcribed into the exact edit it decides.

    Returns `(edit, note)` when the model committed, `(None, reason)` when it
    declined — and a decline is an answer for the designer, not an error. The
    edit still has to anchor and apply like any other; nothing is written
    here.

    `history`, when given, is the earlier turns of a conversation about this
    same flag (see `converse`): the designer's prior messages and the model's
    prior replies. `typed` is then the latest message, carried out in light of
    them, so a follow-up like "no, the other one" resolves against what was
    proposed before."""
    user = _adjudicate_prompt(item, typed, stories, history=history)
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


def _adjudicate_prompt(item: dict, typed: str, stories: list[Story], *,
                       history: Sequence[dict] | None = None) -> str:
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
    if item.get("advice"):
        lines.append(f"- what an earlier read of the whole book advised: "
                     f"{item['advice']}")
    # The placements the screen showed, numbered as the designer saw them — the
    # referent of an answer like "the second one" or "not that one, the other".
    options = item.get("options") or []
    if options:
        lines += ["", "THE PLACEMENTS SHOWN ON SCREEN — when the answer says "
                      "\"the first one\" / \"the second one\", it means these, "
                      "in this order:"]
        for i, o in enumerate(options, 1):
            where = (f"page {o['page_label']}" if o.get("page_label")
                     else f"page {o['page']}" if o.get("page")
                     else f"story {o['story_id']}, paragraph {o['paragraph']}")
            lines.append(f"{i}. [{where}] “{o.get('found', '')}” → "
                         f"“{o.get('replacement', '')}” in: "
                         f"“{(o.get('before') or '')[:300]}”")
    # The conversation so far, when this is a follow-up turn: the designer is
    # refining across messages, and a referent like "the other one" or "also
    # italicize it" only means anything against what was said and proposed
    # before. The latest message is rendered separately below as the decision.
    if history:
        lines += ["", "THE CONVERSATION SO FAR — the designer is refining this "
                      "over several messages; read their latest message (below) "
                      "in light of these earlier turns:"]
        for turn in history:
            role = turn.get("role")
            text = " ".join((turn.get("text") or "").split())
            if role == "designer":
                lines.append(f"- the designer said: “{text}”")
            elif role == "model":
                prop = turn.get("proposal") or {}
                if prop.get("found") is not None and prop.get("replacement") \
                        is not None:
                    lines.append(f"- you proposed: “{prop.get('found', '')}” → "
                                 f"“{prop.get('replacement', '')}”"
                                 + (f" ({text})" if text else ""))
                else:
                    lines.append(f"- you replied: “{text}”")
    lines += ["", ("THE DESIGNER'S LATEST MESSAGE — the decision to carry out:"
                   if history else
                   "THE DESIGNER'S ANSWER — the decision to carry out:"),
              f"“{typed.strip()}”", ""]
    passages = _context_passages(item, stories)
    if passages:
        lines.append("THE BOOK'S OWN TEXT — copy every find and context from "
                     "it, character for character:")
        lines.append("")
        lines.extend(passages)
    if item.get("page_text"):
        lines += [f"THE BOOK'S OWN TEXT FOR PAGE {item.get('page') or '?'} — "
                  "the page the mark was made on:", "", item["page_text"], ""]
    if not passages and not item.get("page_text"):
        lines.append("(No passage of the book could be located for this flag; "
                     "decline unless the designer's answer itself quotes the "
                     "exact book text to change.)")
    return "\n".join(lines)


def _context_passages(item: dict, stories: list[Story]) -> list[str]:
    """The paragraphs the answer is about: the ones the item's options sit in —
    each with the paragraph either side of it, because a decision like "make it
    match the rest of the scene" is about the flow, not one line — or failing
    those the ones its anchor (then its find) is found in. Bounded,
    deduplicated, in reading order."""
    seen: set[tuple[str, int]] = set()
    out: list[str] = []
    total = 0
    by_story = {s.story_id: s for s in stories}

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

    def add_around(story_id: str, index: int) -> None:
        story = by_story.get(story_id)
        if story is None:
            return
        for i in (index - 1, index, index + 1):
            if 0 <= i < len(story.paragraphs):
                add(story_id, i, story.paragraphs[i].text)

    for o in item.get("options") or []:
        add_around(o["story_id"], o["paragraph"])
    if not out:
        cache = IndexCache()
        for probe in (item.get("anchor") or "", item.get("find") or ""):
            if len(probe.strip()) < MIN_FIND:
                continue
            for s in stories:
                for p in s.paragraphs:
                    if all_spans(p.text, probe, cache=cache,
                                 partial_words=True):
                        add_around(s.story_id, p.index)
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

def suggestion_instruction(item: dict) -> str:
    """The typed answer a one-click "apply the suggestion" stands for: carry out
    exactly what the earlier model advice described, nothing more. Raises when
    the item carries no advice to apply."""
    advice = " ".join((item.get("advice") or "").split())
    if not advice:
        raise ResolveError("this flag carries no model suggestion to apply")
    return ("Carry out exactly the change this earlier advice describes — "
            f"nothing more, nothing else: “{advice}”")


# The one-click ask for a flag that carries no stored advice: the designer
# explicitly hands the model the call. It answers with a concrete edit or a
# decline — the same contract as a typed answer, because it goes down the same
# path.
DELEGATED_INSTRUCTION = (
    "Decide this one and carry it out: apply the reviewer's mark at the "
    "placement the evidence supports — the cited page first, then the marked "
    "line. If the evidence does not settle what to change or where, decline "
    "and say what is missing.")


def _edit_to_placement(edit: Edit, stories: list[Story], *, page: int = 0,
                       page_label: str = "", note: str = "") -> dict:
    """Dry-run one adjudicated edit against the corrected book held in `stories`
    and hand it back as the exact span it would change, in the same shape as a
    queue option so the accept-a-placement path (`apply_option`) can write it.

    Nothing is written to disk — `stories` is mutated in memory only, off a
    snapshot taken first, so the caller passes freshly-read stories it then
    discards. Raises ResolveError when the edit does not land as one clean,
    visible span: nowhere, a whole-line removal, or a pure restyle with no
    words changed (each of which the designer settles a different way)."""
    snapshot = {s.story_id: [p.text for p in s.paragraphs] for s in stories}
    outcomes, _ = apply_to_stories(stories, [edit])
    mine = next((o for o in outcomes if o.edit.id == edit.id), None)
    if mine is None or not mine.applied:
        why = (mine.detail or mine.status) if mine is not None else "unknown"
        raise ResolveError(f"the change could not be placed: {why}")
    if any(o.applied and o.edit.id.endswith("-para") for o in outcomes):
        raise ResolveError("that would remove a whole line — use Edit the "
                           "line, or type the answer instead")
    story = next(s for s in stories if s.story_id == mine.story_id)
    lines = snapshot.get(mine.story_id) or []
    if not 0 <= mine.paragraph < min(len(lines), len(story.paragraphs)):
        raise ResolveError("the change could not be shown — type it as an "
                           "answer instead")
    before = lines[mine.paragraph]
    after = story.paragraphs[mine.paragraph].text
    if before == after:
        raise ResolveError("that only re-styles the text — use Edit the line "
                           "to set it by hand")
    p = 0
    while p < len(before) and p < len(after) and before[p] == after[p]:
        p += 1
    s = 0
    while (s < len(before) - p and s < len(after) - p
           and before[len(before) - 1 - s] == after[len(after) - 1 - s]):
        s += 1
    row = {
        "id": "",                     # numbered into the item by the caller
        "edit_id": "",
        "story_id": mine.story_id,
        "paragraph": mine.paragraph,
        "start": p, "end": len(before) - s,
        "found": before[p:len(before) - s],
        "replacement": after[p:len(after) - s],
        "before": before, "after": after,
        "page": page,
        "note": note,
    }
    # The folio the finished IDML shows for that page, exactly as every other
    # placement row carries it — a placement's "page 43" must be the same 43.
    if page_label:
        row["page_label"] = page_label
    return row


def materialize_suggestion(item: dict, corrected: str | Path,
                           provider: Provider, *, model: str,
                           usage: Usage) -> dict:
    """One model call turned into a clickable placement: the flag's stored
    advice — or a delegated ask when it has none — adjudicated into an edit,
    dry-run against the corrected book in memory, and handed back as the exact
    span it would change, in the same shape as a queue option. Nothing is
    written; the designer accepts it by clicking, exactly like any other
    placement. A decline, or a suggestion that cannot be shown as one clean
    span, is refused with the reason."""
    typed = (suggestion_instruction(item) if item.get("advice")
             else DELEGATED_INSTRUCTION)
    stories = read_stories(corrected)
    edit, note = adjudicate_instruction(item, typed, provider, model=model,
                                        usage=usage, stories=stories)
    if edit is None:
        raise ResolveError(f"the model declined — {note}")
    row = _edit_to_placement(edit, stories, page=item.get("page") or 0,
                             page_label=item.get("page_label", ""), note=note)
    row["suggested"] = True
    return row


def converse(item: dict, corrected: str | Path, provider: Provider, *,
             model: str, usage: Usage) -> dict:
    """One turn of a per-flag conversation. Reads the whole thread off
    `item["chat"]` — whose last entry is the designer's just-added message —
    re-runs adjudication over it, and, when the result is a change that lands,
    dry-runs it into a proposal the designer can accept. Returns
    `{"reply": <the model's words>, "proposal": <placement> | None}`; writes
    nothing. A decline, or a change that cannot be shown as one clean span, is
    not an error here — it comes back as a reply the designer answers, so the
    conversation continues rather than stopping."""
    chat = item.get("chat") or []
    if not chat or chat[-1].get("role") != "designer":
        raise ResolveError("there is no message to answer")
    typed = (chat[-1].get("text") or "").strip()
    history = chat[:-1]
    stories = read_stories(corrected)
    edit, note = adjudicate_instruction(item, typed, provider, model=model,
                                        usage=usage, stories=stories,
                                        history=history)
    if edit is None:
        return {"reply": note or ("I couldn't turn that into a change — say it "
                                  "differently, or name the exact words."),
                "proposal": None}
    try:
        proposal = _edit_to_placement(
            edit, stories, page=item.get("page") or 0,
            page_label=item.get("page_label", ""), note=note)
    except ResolveError as e:
        return {"reply": (f"{note} " if note else "")
                + f"But I couldn't place it here: {e}", "proposal": None}
    proposal["from_chat"] = True
    return {"reply": note or "Here's the change — accept it, or tell me what to "
                             "adjust.",
            "proposal": proposal}


def queue_counts(payload: dict) -> dict:
    """The card's counters, read off the report as it now stands: edits applied
    (the run's plus every resolution), flagged edits still awaiting someone, and
    reviewer comments still a person's. The same numbers the run computed,
    re-derived so a resolution — or a deliberate set-aside — moves them."""
    ap = payload.get("apply") or {}
    flagged = [o for o in (ap.get("flagged") or [])
               if not o.get("resolved") and not o.get("dismissed")]
    com = payload.get("comments") or {}
    unresolved = [c for c in (com.get("items") or [])
                  if c["disposition"] in (DISP_FLAGGED, DISP_NOT_EXTRACTED)
                  and not c.get("resolved") and not c.get("dismissed")]
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
    dismissed = resolution.get("kind") == "dismissed"

    ap = payload.get("apply")
    if ap is not None and dismissed:
        # A set-aside applies nothing; it moves the item's flags out of the
        # awaiting pile into their own bucket, so the counts say what the
        # designer decided rather than pretending the work was done.
        for o in ap.get("flagged") or []:
            if o["id"] in (item.get("edit_ids") or []):
                o["dismissed"] = True
    elif ap is not None:
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
                c["dismissed" if dismissed else "resolved"] = True
        _recount_unresolved(com)
    if not dismissed:
        note = ("resolved in review by the designer"
                + (f" — “{resolution['text']}”" if resolution.get("text")
                   else ""))
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
    _write_updated(json_path, payload)
    return payload


def record_touchup(json_path: str | Path, resolution: dict) -> dict:
    """Fold a hand edit that answers no flag — the designer touching up an
    *applied* correction from the review list — into the report. It joins the
    resolutions (so the log says who edited what) and the changes list (so the
    printable review shows the line as it now reads), but moves no counter: it
    resolved nothing and applied no correction from the list, and the
    reconciliation treats it accordingly."""
    json_path = Path(json_path)
    payload = json.loads(json_path.read_text("utf-8"))
    resolution = dict(resolution)
    resolution["at"] = datetime.now(timezone.utc).isoformat()
    resolution["kind"] = "manual"
    resolution["touchup"] = True
    payload.setdefault("resolutions", []).append({"item_id": "", **resolution})
    payload.setdefault("changes", []).append({
        "story_id": resolution.get("story_id", ""),
        "paragraph": resolution.get("paragraph", -1),
        "before": resolution.get("before", ""),
        "after": resolution.get("after", ""),
        "edit_ids": [],
        "instruction": "edited by hand in review",
        "formatting": "",
        "resolved_in_review": True,
    })
    _write_updated(json_path, payload)
    return payload


def reopen_dismissed(json_path: str | Path, item_id: str) -> dict:
    """Put a set-aside flag back in the awaiting pile — the undo a one-click
    "ignore" deserves. Only a dismissal can be reopened: it wrote nothing to the
    file, so unwinding it is pure bookkeeping; an applied resolution changed the
    book and stays."""
    json_path = Path(json_path)
    payload = json.loads(json_path.read_text("utf-8"))
    item = next((q for q in payload.get("queue") or []
                 if q.get("id") == item_id), None)
    if item is None:
        raise ResolveError("this flag is not in the report — reload it and "
                           "try again")
    resolved = item.get("resolved") or {}
    if resolved.get("kind") != "dismissed":
        raise ResolveError("only an ignored flag can be put back — an applied "
                           "change is in the file")
    item["resolved"] = None
    ap = payload.get("apply")
    if ap is not None:
        for o in ap.get("flagged") or []:
            if o["id"] in (item.get("edit_ids") or []):
                o.pop("dismissed", None)
    com = payload.get("comments") or {}
    if item.get("comment_id"):
        for c in com.get("items") or []:
            if c["id"] == item["comment_id"]:
                c.pop("dismissed", None)
        _recount_unresolved(com)
    payload["resolutions"] = [r for r in payload.get("resolutions") or []
                              if not (r.get("item_id") == item_id
                                      and r.get("kind") == "dismissed")]
    _write_updated(json_path, payload)
    return payload


def _recount_unresolved(com: dict) -> None:
    com["unresolved"] = sum(
        1 for c in com.get("items") or []
        if c["disposition"] in (DISP_FLAGGED, DISP_NOT_EXTRACTED)
        and not c.get("resolved") and not c.get("dismissed"))


def _write_updated(json_path: Path, payload: dict) -> None:
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path = json_path.parent / "corrections_notes.md"
    try:
        from .report import _markdown
        md_path.write_text(_markdown(payload), encoding="utf-8")
    except Exception:                  # noqa: BLE001 - the JSON is the record
        log.warning("Could not re-render %s after a resolution", md_path,
                    exc_info=True)
