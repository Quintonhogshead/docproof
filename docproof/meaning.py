"""The meaning gate: one strong model reading every change before it ships.

Every other check in the pipeline asks some version of "is this an error, and is
the fix right?" — the detector's own prompts, the confirm valve the recall passes
share, the overseer-verifier, the between-round judge. None of them asks the
question an author actually cares about: *does the corrected sentence still say
what the original said?* A fix can be defensible as grammar and still change the
book — a dropped negation, a homophone "corrected" the wrong way, a tense moved
so an event lands before the scene instead of after it.

This pass asks only that question, once, over every proposed change from every
source at the same time. It is the last thing to run before the tracked changes
are written, which is what lets it be source-blind: by the time it reads them, a
Sapling suggestion, a LanguageTool comma, a rewrite diff, and a detector finding
are all just "a span of this paragraph, and what it would become". One umbrella,
one question, one verdict.

Three verdicts, and the routing is deliberately asymmetric:

  preserves — the sentence still means what it meant; the change applies
  changes   — the meaning moved; the change is downgraded to a margin question
  unsure    — cannot tell; treated as `changes`, because the cost of asking is a
              comment and the cost of being wrong is a corrupted sentence

Nothing is ever dropped: a flagged change becomes a question in the margin with
the judge's reason attached, so the author sees both the proposed fix and why it
was held back. Fail-open on everything else — a refusal, a truncation, or an
unparseable answer leaves the change exactly as it was, because a judge that
could not answer must never be able to silently delete a correction.

Cost scales with the number of changes, not the length of the book: a manuscript
with 500 accepted edits spread over 400 paragraphs is 400 short calls, which is
why a frontier model is affordable here and nowhere else in the pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Literal, Sequence

from pydantic import BaseModel, ValidationError

from .models import Finding, Usage
from .providers import Provider, strict_json_schema

log = logging.getLogger("docproof.meaning")


_DEFAULT_MEANING_PROMPT = """\
You are a senior editor performing the LAST check before corrections reach an \
author's manuscript. Every change below has already been judged a legitimate \
correction by earlier reviewers. You are not re-judging that. You are asking one \
question, and only this one:

    Does the corrected sentence still mean what the original meant?

For each change you get the paragraph it sits in, the sentence as the author \
wrote it, and the sentence as it would read after the change. Return exactly one \
verdict per numbered item:

- preserves: the meaning is intact. The change fixes form — spelling, \
punctuation, agreement, a missing function word, a clear typo — and the sentence \
still asserts the same thing about the same people, in the same order, with the \
same certainty and the same time frame.
- changes: the meaning moved. Some examples of what that looks like: a negation \
added or lost ("could" to "couldn't"); a homophone resolved the wrong way for \
this context ("bared" to "barred" where the author meant the first); a pronoun \
repointed to a different character; a tense or aspect shift that moves when an \
event happened; a hedge or intensifier added or removed ("might have" to "had"); \
a substituted word whose sense is different rather than merely better; a comma \
or clause boundary that changes who did what to whom.
- unsure: the paragraph in front of you genuinely does not settle it — you would \
need to see more of the book to know whether the sense survived. This is not a \
hedge: if you can answer, answer.

Read those examples as descriptions of a SHIFT, not of a category of edit. \
Correcting a verb to agree with the subject the author already wrote, or a \
pronoun to match the antecedent already named, or a tense to match the one the \
passage is already in, changes the form and leaves the sense where it was — that \
is "preserves". It is "changes" only when the corrected sentence points at a \
different referent, a different time, or a different degree of certainty than \
the original did.

Judge the SENSE, not the style. A correction can be blunt, plain, or not how you \
would have phrased it and still preserve meaning exactly — that is "preserves". \
Do not flag a change merely because it is unnecessary, inelegant, or because you \
would have fixed it differently; another reviewer already ruled on whether it \
should happen at all.

Fiction has meaning beyond the literal, so a change can leave the facts intact \
and still take something away: a character's dialect or idiolect flattened into \
standard English, a deliberate fragment made into a full sentence, a stylized or \
archaic spelling normalized, an invented term replaced with a real word. In \
dialogue especially, how a person speaks is part of what the sentence says about \
them. Flag one of these only when the voice actually changes on the page — a \
plain misspelling inside a line of dialogue is still a misspelling, and fixing \
it is "preserves".

House style is not your call. Spacing around ellipses and em dashes, serial \
commas, capitalization conventions, how numbers and dialogue punctuation are \
set — those are settled elsewhere, and a change that applies one of them \
preserves meaning by definition. Never flag a house-style change.

Deleting or inserting a comma, curling a quotation mark, fixing a plainly \
misspelled word, and correcting subject-verb agreement are ordinarily \
"preserves" — say so plainly rather than hunting for a reading in which they \
are not.

Calibrate accordingly. These are the corrections of an ordinary proofread, so \
the overwhelming majority of them do preserve meaning and the honest answer for \
nearly all of them is "preserves". You are here for the rare one that slipped \
through — reserve "changes" and "unsure" for a real shift in sense you could \
name to the author in one sentence, not for a theoretical reading nobody would \
take. Flagging a routine fix is not caution; it buries the one that matters \
under questions the author has to dismiss.

When a change would alter the sense, or you cannot tell, say so: a margin \
question costs the author a moment, and a silently altered sentence costs them \
their book. Put one short sentence of reason on every "changes" and "unsure" — \
it is what the author reads in the margin — naming what the sense shifted from \
and to. Leave reason empty for "preserves".

Return an object with a "verdicts" array holding one entry per numbered item \
given, using verdict values exactly "preserves", "changes", or "unsure"."""


def default_meaning_prompt() -> str:
    """The built-in instructions — for the review panel to pre-fill its editable
    field and to fall back to when that field is cleared."""
    return _DEFAULT_MEANING_PROMPT


class _MVerdict(BaseModel):
    # The item's number in the list it was given, 1-based. A number rather than a
    # finding_id on purpose: ids are only unique per source (the consistency scan
    # and continuity both mint "c-0001"), so keying answers by id lets one
    # finding collect another's verdict — and a verdict landing on the wrong
    # change is exactly the failure this pass exists to prevent.
    item: int
    verdict: Literal["preserves", "changes", "unsure"]
    reason: str            # one short sentence; the margin note on a downgrade


class _MVerdicts(BaseModel):
    verdicts: list[_MVerdict]


def _change_view(f: Finding, para_text: str) -> tuple[str, str]:
    """The sentence the change sits in, before and after it.

    Every finding that reaches this pass has already been validated, so it
    carries the exact minimal edit as an anchor — which is better to render from
    than the finding's own quote. Some sources (the rewrite pass especially)
    quote a whole paragraph, and the paragraph is already at the top of the
    prompt: repeating it twice per item would triple the tokens and bury a
    two-character fix inside two walls of identical prose. Falls back to the
    finding's own quote when there is no anchor to cut from."""
    a = f.anchor
    if a is None or para_text[a.start:a.end] != a.delete_text:
        return f.original_text, f.corrected_text
    from .sweeps import sentence_window
    window, lo, _ = sentence_window(para_text, a.start, a.end)
    after = window[:a.start - lo] + a.insert_text + window[a.end - lo:]
    return window, after


@dataclass(frozen=True)
class MeaningReport:
    """What the gate did, for the caller and the run report. `downgraded` are the
    findings whose meaning the judge would not vouch for, already rewritten with
    force_query and the judge's reason; `checked` counts the changes actually
    put in front of the model, and `calls` the paragraphs it took to do it.

    `positions` are where each downgraded finding sat in the list handed to
    `screen_meaning`, so a caller can put it back exactly. Position rather than
    finding_id on purpose: ids are only unique per source (continuity and the
    consistency scan both mint "c-0001"), and swapping the wrong finding into a
    manuscript is precisely the failure this pass exists to prevent.

    `answered` counts the changes that actually came back with a verdict. It is
    reported rather than inferred because the pass fails open: a judge that
    refused every call downgrades nothing, which is indistinguishable from a
    judge that read everything and approved it — unless the run says which."""
    downgraded: list[Finding]
    positions: tuple[int, ...]
    checked: int
    calls: int
    answered: int = 0

    @property
    def n_downgraded(self) -> int:
        return len(self.downgraded)

    @property
    def unread(self) -> int:
        """Changes that went out with no verdict on them — applied unread."""
        return max(0, self.checked - self.answered)


class MeaningJudge:
    """One paragraph's proposed changes in, verdicts out. `fetch` is pure — no
    shared state, no usage bookkeeping — so paragraphs judge concurrently and the
    caller folds the results in serially."""

    def __init__(self, provider: Provider, model: str, *,
                 instructions: str = "", context: str = "",
                 max_tokens: int = 4000):
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        parts = [instructions.strip() or _DEFAULT_MEANING_PROMPT]
        if context:
            parts.append(context)
        self.system_prompt = "\n\n".join(parts)
        self.schema = strict_json_schema(_MVerdicts)

    def _render(self, para_text: str, findings: Sequence[Finding]) -> str:
        lines = [f"PARAGRAPH:\n{para_text}\n", "PROPOSED CHANGES:"]
        for n, f in enumerate(findings, start=1):
            # Numbered, and deliberately bare: the error type and the rule that
            # produced the change are withheld, because naming them invites the
            # judge to re-litigate whether the change is correct — which the
            # earlier passes already settled — instead of answering the only
            # question asked here.
            before, after = _change_view(f, para_text)
            lines.append(
                f"- item {n}\n"
                f"  as written: {before!r}\n"
                f"  as corrected: {after!r}")
        lines.append("\nReturn a verdict for every item number above.")
        return "\n".join(lines)

    def fetch(self, para_text: str, findings: Sequence[Finding]):
        return self.provider.complete_structured(
            model=self.model, system=self.system_prompt,
            user=self._render(para_text, findings),
            schema=self.schema, schema_name="verdicts",
            max_tokens=self.max_tokens)

    def parse(self, result, findings: Sequence[Finding]) -> dict[int, dict]:
        """Verdicts keyed by 0-based offset into `findings`. A judge that
        refused, truncated, or returned junk yields nothing — the caller then
        leaves those changes exactly as they were, so a hiccup here can never
        silently downgrade or drop a correction. An item number outside the list
        it was given is dropped rather than wrapped around."""
        if result.stop_reason != "ok" or result.parsed is None:
            log.error("meaning gate: no usable answer for %d change(s): %s",
                      len(findings), result.error or result.stop_reason)
            return {}
        try:
            parsed = _MVerdicts.model_validate(result.parsed)
        except ValidationError as e:
            log.error("meaning gate: answer did not match the schema: %s", e)
            return {}
        return {v.item - 1: v.model_dump()
                for v in parsed.verdicts if 1 <= v.item <= len(findings)}


def _margin_note(f: Finding, reason: str) -> str:
    """What the author reads beside a held-back change: the correction that was
    proposed, then why it was not made. The proposal comes from the finding's own
    explanation where it has one — that is the sentence the pass that proposed it
    would have shown — and falls back to naming the change itself."""
    proposed = (f.explanation or "").strip().rstrip(".")
    if not proposed:
        proposed = f"A correction was proposed here"
    return f"{proposed}. Not applied: {reason}"


def screen_meaning(findings: Sequence[Finding], para_text: dict[str, str],
                   provider: Provider, *, model: str, instructions: str = "",
                   context: str = "", max_tokens: int = 4000,
                   concurrency: int = 8, flag_unsure: bool = True,
                   usage: Usage) -> MeaningReport:
    """Read every change in `findings` and return the ones that move the meaning.

    The caller passes only the changes that would actually become tracked edits
    — a question changes no text, so there is no meaning to preserve, and a
    formatting mark changes no characters at all. Each returned finding carries
    `force_query` and the judge's reason, so re-validating the run's findings
    routes it to the margin instead of the manuscript.

    `flag_unsure` treats an "unsure" verdict as a downgrade, which is the default
    and the safe reading: the gate exists to stop a silent change, and a judge
    that cannot vouch for the sense has not vouched for it."""
    from concurrent.futures import ThreadPoolExecutor

    # Findings are grouped by paragraph but tracked by position, so a verdict
    # always lands back on the finding that was judged.
    by_para: dict[str, list[tuple[int, Finding]]] = {}
    for i, f in enumerate(findings):
        if f.para_id in para_text:              # no paragraph -> nothing to read
            by_para.setdefault(f.para_id, []).append((i, f))
    if not by_para:
        return MeaningReport([], (), 0, 0)

    judge = MeaningJudge(provider, model, instructions=instructions,
                         context=context, max_tokens=max_tokens)
    # Verdicts come back keyed by their offset in the batch, which maps straight
    # onto the caller's positions — no finding_id anywhere in the round trip.
    verdicts: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(items, pool.submit(judge.fetch, para_text[pid],
                                       [f for _, f in items]))
                   for pid, items in by_para.items()]
        for items, fut in pending:   # fold serially: usage.add is not thread-safe
            result = fut.result()
            usage.add(result.usage)
            for offset, v in judge.parse(result, [f for _, f in items]).items():
                verdicts[items[offset][0]] = v

    flagged = {"changes"} | ({"unsure"} if flag_unsure else set())
    downgraded: list[Finding] = []
    positions: list[int] = []
    for pos, f in enumerate(findings):
        v = verdicts.get(pos)
        if v is None or v["verdict"] not in flagged:   # fail open on no answer
            continue
        reason = (v.get("reason") or "").strip()
        note = reason or ("This correction may change the sentence's meaning."
                          if v["verdict"] == "changes" else
                          "Unable to confirm this correction keeps the "
                          "sentence's meaning.")
        # The margin comment has to answer two questions, because it is all the
        # author gets: what was going to be changed here, and why it wasn't. The
        # finding's own explanation carries the first — dropping it would leave a
        # note that says a correction was withheld without ever saying which.
        downgraded.append(replace(f, force_query=True,
                                  explanation=_margin_note(f, note)))
        positions.append(pos)

    checked = sum(len(items) for items in by_para.values())
    if len(verdicts) < checked:
        # Fail-open means those changes were applied unread. Say so loudly here
        # and in the run report — a silent gate reads exactly like a clean one.
        log.warning("Meaning gate: only %d of %d change(s) got a verdict; the "
                    "other %d were applied unread.",
                    len(verdicts), checked, checked - len(verdicts))
    if downgraded:
        log.info("Meaning gate: %d of %d change(s) held back as questions "
                 "(%d paragraph call(s)).", len(downgraded), checked, len(by_para))
    else:
        log.info("Meaning gate: all %d read change(s) preserve meaning "
                 "(%d paragraph call(s)).", len(verdicts), len(by_para))
    return MeaningReport(downgraded, tuple(positions), checked, len(by_para),
                         answered=len(verdicts))
