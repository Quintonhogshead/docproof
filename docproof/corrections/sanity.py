"""An optional model gate over the proposed edits, before any is applied.

Every correction here is the author's own, marked on a proof and approved — so
this gate is emphatically *not* an editor second-guessing the calls. It does not
ask whether an edit is the change the reviewer "really meant", whether the word
fits the register, or whether the world of the book would carry it. All of that
is settled: the mark is the instruction, and the instruction is to be carried
out.

The one thing an author's approval cannot vouch for is a mechanical slip in
carrying the mark out — an edit that, applied exactly as written, leaves a
sentence a reader would trip over. A find that reached past the word that changes
and dropped a verb with it ("slowly eased" → "unhurriedly"); a punctuation mark
moved so it splits a sentence or leaves a dangling comma, an unclosed dash, a
stray capital mid-clause; a reviewer's shorthand ("been") applied to the letter
where the sentence needed it expanded ("have been"). None of these is a
judgment call. Each is a broken sentence, and a broken sentence in a printed book
is the one outcome worth holding a correction back over.

So the gate reads each proposed edit beside the line it changes and asks a single,
narrow question: **applied as written, does this leave a grammatically broken or
incoherent sentence?** If yes, it hands the edit id back to be withheld, with the
break named and the grammatical repair spelled out, so a person can set it right
in a moment. If no — including every edit it merely finds inelegant, unexpected,
or not to its taste — it lets the edit through untouched. Put the fries in the
basket: apply the author's marks, and stop only for a sentence that will not
stand up.

It never *makes* an edit and never rewrites one; a false alarm costs a second
look, never a wrong change shipped. Opt-in, and a gate that itself fails (a
refusal, a truncation) withholds nothing and lets the run proceed — a broken
check must not sink the corrections it was checking.
"""
from __future__ import annotations

import logging
from typing import Callable, Literal, Sequence

from pydantic import BaseModel

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .model import DESIGN, Edit

log = logging.getLogger("docproof.corrections.sanity")

MAX_OUTPUT_TOKENS = 8000
# Judge a bounded batch at a time: a big proof has hundreds of edits, and a
# structured reply that overruns the output ceiling parses as nothing — which
# would withhold nothing and silently wave the whole batch through.
BATCH_SIZE = 40

# The one verdict that holds an edit back. "ok" lets it apply — and "ok" is the
# answer for everything except a sentence that is grammatically broken as written.
WITHHOLD = {"broken"}


class _Verdict(BaseModel):
    id: str
    verdict: Literal["ok", "broken"]
    reason: str = ""


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


_SYSTEM = """\
You are a proofreader's last mechanical check on a list of corrections an author \
marked on a proof of their own book. The corrections are APPROVED — the mark is \
the instruction, and it is to be carried out. You are given each edit as an exact \
find → replace against the line it changes, with the reviewer's note.

When several corrections are listed for one line they are applied TOGETHER, and \
the line after all of them is given to you. Judge that line by how it reads once \
EVERY correction on it has run — never one of them in isolation. A quotation \
whose opening mark is fixed by one correction and closing mark by another is \
balanced, not broken; a tense fixed across two verbs by two corrections agrees, \
not clashes. Hold an edit back only when the line is still broken after all of \
its siblings have been applied.

You are NOT judging the correction. Do not ask whether it is the change the \
reviewer meant, whether the wording fits the book's register, whether it is an \
anachronism, whether a dialect spelling is "wrong", or whether a different edit \
would read better. Every one of those is the author's call and is already made. \
Second-guessing them is not your job and is a real cost — it holds up an approved \
correction over a matter of taste.

Your one job is to catch a MECHANICAL slip in how the mark was carried out: an \
edit that, applied exactly as written, leaves a sentence that is grammatically \
broken or incoherent — the kind of thing a reader would catch in the printed \
book. Return one verdict per edit:

- "broken": applied as written, the edit leaves a sentence that does not stand \
up grammatically. The clear cases:
  · a dropped or garbled word — the find reached past the word that changes and \
took a verb, object or phrase with it, so the result is missing a word or does \
not parse ("slowly eased" → "unhurriedly", losing "eased").
  · a punctuation mark changed in a way that fractures the sentence — a full stop \
dropped into the middle of one clause, an independent clause spliced to another by \
a comma, a dash or a quotation mark opened and never closed, a stray capital left \
mid-sentence. But judge a mark against the whole passage, not the fragment you \
were handed: a replacement that inserts a block quotation, an epigraph or a run of \
verse may carry a quotation mark whose partner sits in text outside the line \
shown, or open a quotation the block itself closes further down — that is not a \
break. Only call a quotation unbalanced when a single ordinary prose sentence, \
read whole, is plainly left with a dangling mark.
  · the reviewer's shorthand applied to the letter where the grammar of the \
sentence needed it expanded — "been" written where the past conditional needs \
"have been" ("I'd been forced" for "I'd have been forced"); a bare word swap that \
leaves the article, number or verb around it disagreeing.
  · the replacement is not real language, or makes the sentence incoherent.
  When you answer "broken", name the break and give the grammatical repair in the \
reason, so a person can fix it at a glance.
- "ok": the sentence stands up. Use this for everything else — including every \
edit you merely find inelegant, surprising, unnecessary, or not to your taste. A \
grammatical sentence is "ok" even if you would not have made the change. When in \
doubt, "ok": the author approved it, and only a genuinely broken sentence is \
worth stopping for.

Judge only what is in front of you; do not invent problems. Return a verdict for \
every edit id, using the exact id given."""


def _line_key(e: Edit) -> str:
    """The line an edit sits on, as the key co-located edits share. Empty when the
    edit carries no context and no find, which makes it its own group."""
    return (e.context or e.find or "").strip()


def _groups(edits: Sequence[Edit]) -> list[list[Edit]]:
    """`edits` gathered into the lines they sit on, in first-seen order. Two edits
    on one line are the pair whose halves must be judged together, not apart —
    which is the whole point of this grouping."""
    groups: list[list[Edit]] = []
    index: dict[str, int] = {}
    for e in edits:
        key = _line_key(e)
        if key and key in index:
            groups[index[key]].append(e)
            continue
        if key:
            index[key] = len(groups)
        groups.append([e])
    return groups


def _batched_groups(groups: list[list[Edit]], size: int):
    """Groups packed into batches of at most `size` edits, never splitting a group
    across a batch — a line's edits must reach the model in one call, or the gate
    is judging a half of it in isolation again."""
    batch: list[list[Edit]] = []
    count = 0
    for g in groups:
        if batch and count + len(g) > size:
            yield batch
            batch, count = [], 0
        batch.append(g)
        count += len(g)
    if batch:
        yield batch


def _apply_all(line: str, edits: Sequence[Edit]) -> str:
    """`line` with every edit on it applied, left to right — the line the reader
    will see, shown to the gate so it judges the result and not an intermediate.
    A best-effort render: each find is replaced once, and one already consumed by
    an earlier sibling is skipped, exactly as `apply` would leave it."""
    out = line
    for e in edits:
        if e.find and e.find in out:
            out = out.replace(e.find, e.replace, 1)
    return out


def _format(groups: Sequence[Sequence[Edit]]) -> str:
    lines = ["Proposed corrections:", ""]
    for g in groups:
        line = g[0].context or g[0].find
        if len(g) == 1:
            e = g[0]
            lines.append(f"- id {e.id}: on the line \"{line}\"")
            lines.append(f"    change \"{e.find}\" to \"{e.replace}\""
                         + (f"  (note: {e.instruction})" if e.instruction else ""))
            continue
        lines.append(f"- on the line \"{line}\", these corrections are applied "
                     f"together:")
        for e in g:
            lines.append(f"    · id {e.id}: change \"{e.find}\" to \"{e.replace}\""
                         + (f"  (note: {e.instruction})" if e.instruction else ""))
        after = _apply_all(line, g)
        if after != line:
            lines.append(f"    after all of them the line reads: \"{after}\"")
    return "\n".join(lines)


def review_edits(edits: Sequence[Edit], provider: Provider, *, model: str,
                 usage: Usage, max_tokens: int = MAX_OUTPUT_TOKENS,
                 progress: Callable[[int, int], None] | None = None
                 ) -> dict[str, str]:
    """The edit ids the gate holds back, each mapped to a short reason.

    Only real text edits are judged — a design route or a no-op has nothing to
    apply. `usage` accrues the model spend. A batch whose model call fails is
    logged and skipped (nothing withheld from it), so the gate degrades to
    letting edits through rather than blocking the run.

    `progress(done, total)`, when given, is called as each batch is reached and
    once at the end, so a caller (the app's job card) can show the gate moving
    instead of one long silence."""
    candidates = [e for e in edits if e.kind != DESIGN and e.find != e.replace]
    total = len(candidates)
    withheld: dict[str, str] = {}
    done = 0
    for batch in _batched_groups(_groups(candidates), BATCH_SIZE):
        if progress:
            progress(done, total)
        done += sum(len(g) for g in batch)
        try:
            result = provider.complete_structured(
                model=model, system=_SYSTEM, user=_format(batch),
                schema=strict_json_schema(_Verdicts), schema_name="edit_sanity",
                max_tokens=max_tokens)
        except Exception:                         # noqa: BLE001 - a gate must not sink the run
            log.warning("Sanity gate call failed; letting this batch through",
                        exc_info=True)
            continue
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            log.warning("Sanity gate returned no usable answer (%s); letting "
                        "this batch through", result.stop_reason)
            continue
        for v in result.parsed.get("verdicts", []):
            eid, verdict = v.get("id"), v.get("verdict")
            if eid and verdict in WITHHOLD:
                reason = (v.get("reason") or "").strip()
                withheld[eid] = f"{verdict}: {reason}" if reason else verdict
    if progress:
        progress(total, total)
    if withheld:
        log.info("Sanity gate held back %d of %d edit(s) for a human",
                 len(withheld), len(candidates))
    return withheld
