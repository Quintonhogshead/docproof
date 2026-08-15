"""The judge gates: strong models reading every change before it ships.

Every other check in the pipeline asks some version of "is this an error, and is
the fix right?" while the change is still being proposed. These run at the other
end, over the changes that survived everything else, and they are the last thing
between a correction and an author's manuscript. Two of them ship:

  meaning — does the corrected sentence still SAY what the original said?
  fix     — is this replacement actually the RIGHT correction?

They are deliberately separate questions. A change can be a faithful fix that
alters the sense ("could" to "couldn't" where the author meant the first), and it
can preserve the sense perfectly while being the wrong repair ("their" to "there"
where "they're" was wanted). One judge asked to weigh both at once does neither
well, so each gets its own pass, its own prompt, and its own model.

Everything else about them is shared, and lives here: paragraph batching, the
numbered-item wire protocol, fail-open parsing, concurrency, and the verdict
routing. A new judge is a `JudgeSpec` and a prompt — no new machinery.

Both gates are purely SUBTRACTIVE. A change a judge will not vouch for becomes a
margin question carrying what was proposed and why it was withheld; nothing is
ever dropped, nothing is ever added, and no span moves. A judge that refuses,
truncates, or answers unusably leaves the change exactly as it was — which means
a broken judge is a judge that did nothing, never one that quietly deleted a
correction. The run reports how many changes actually got a verdict so a silent
gate cannot be mistaken for a clean one.

Cost scales with the number of changes, not the length of the book: a manuscript
with 500 accepted edits over 400 paragraphs is 400 short calls per gate, which is
why a frontier model is affordable here and nowhere else in the pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Literal, Sequence

from pydantic import BaseModel, ValidationError

from .models import Finding, Usage
from .providers import Provider, strict_json_schema

log = logging.getLogger("docproof.judges")


# --- the wire protocol -------------------------------------------------------
#
# One neutral vocabulary for every judge: the question is what differs, and the
# prompt owns that. `keep` and `withhold` name the CONSEQUENCE rather than the
# reasoning, which keeps one schema (and one parser) correct for any judge that
# is ever added, and states plainly to the model what its answer will do.

class _Verdict(BaseModel):
    # The item's number in the list it was given, 1-based. A number rather than a
    # finding_id on purpose: ids are only unique per source (the consistency scan
    # and continuity both mint "c-0001"), so keying answers by id lets one
    # finding collect another's verdict — and a verdict landing on the wrong
    # change is exactly the failure these passes exist to prevent.
    item: int
    verdict: Literal["keep", "withhold", "unsure"]
    reason: str            # one short sentence; the margin note on a withhold


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


_PROTOCOL = """\
Answer for each numbered item with exactly one of:

- keep: the change is sound on the question asked above. Say this plainly and \
often — it is the honest answer for nearly every correction in an ordinary \
proofread.
- withhold: the change fails the question above. It will NOT be applied; it \
becomes a question in the margin for the author instead.
- unsure: the paragraph in front of you genuinely does not settle it. This is \
not a hedge — if you can answer, answer.

Put one short sentence of reason on every "withhold" and "unsure" — it is what \
the author reads in the margin. Leave reason empty for "keep".

Return an object with a "verdicts" array holding one entry per numbered item \
given, using verdict values exactly "keep", "withhold", or "unsure"."""


@dataclass(frozen=True)
class JudgeSpec:
    """One judge: what it is called, what it asks, and what it says when it
    withholds without giving a reason. The machinery below is identical for
    every spec — adding a judge is adding one of these."""
    key: str                   # "meaning" | "fix"; also the config/record key
    label: str                 # for logs and the run report
    question: str              # one line, quoted in the report
    prompt: str                # the built-in system prompt
    withhold_note: str         # margin note when the model gives no reason
    unsure_note: str


# --- the meaning judge -------------------------------------------------------

_MEANING_PROMPT = """\
You are a senior editor performing the LAST check before corrections reach an \
author's manuscript. Every change below has already been judged a legitimate \
correction by earlier reviewers, and another pass is checking separately whether \
each fix is technically right. You are not doing either of those. You are asking \
one question, and only this one:

    Does the corrected sentence still mean what the original meant?

For each change you get the paragraph it sits in, the sentence as the author \
wrote it, and the sentence as it would read after the change.

Keep the change when the meaning is intact: it fixes form — spelling, \
punctuation, agreement, a missing function word, a clear typo — and the sentence \
still asserts the same thing about the same people, in the same order, with the \
same certainty and the same time frame.

Withhold it when the meaning moved. Some examples of what that looks like: a \
negation added or lost ("could" to "couldn't"); a homophone resolved the wrong \
way for this context ("bared" to "barred" where the author meant the first); a \
pronoun repointed to a different character; a tense or aspect shift that moves \
when an event happened; a hedge or intensifier added or removed ("might have" to \
"had"); a substituted word whose sense is different rather than merely better; a \
comma or clause boundary that changes who did what to whom.

Read those examples as descriptions of a SHIFT, not of a category of edit. \
Correcting a verb to agree with the subject the author already wrote, or a \
pronoun to match the antecedent already named, or a tense to match the one the \
passage is already in, changes the form and leaves the sense where it was — keep \
those. Withhold only when the corrected sentence points at a different referent, \
a different time, or a different degree of certainty than the original did.

Judge the SENSE, not the style. A correction can be blunt, plain, or not how you \
would have phrased it and still preserve meaning exactly. Do not withhold a \
change merely because it is unnecessary, inelegant, or not how you would have \
fixed it.

Fiction has meaning beyond the literal, so a change can leave the facts intact \
and still take something away: a character's dialect or idiolect flattened into \
standard English, a deliberate fragment made into a full sentence, a stylized or \
archaic spelling normalized, an invented term replaced with a real word. In \
dialogue especially, how a person speaks is part of what the sentence says about \
them. Withhold one of these only when the voice actually changes on the page — a \
plain misspelling inside a line of dialogue is still a misspelling, and fixing it \
keeps the meaning.

House style is not your call. Spacing around ellipses and em dashes, serial \
commas, capitalization conventions, how numbers and dialogue punctuation are \
set — those are settled elsewhere, and a change that applies one of them \
preserves meaning by definition. Never withhold a house-style change.

Calibrate accordingly. These are the corrections of an ordinary proofread, so \
the overwhelming majority of them do preserve meaning. You are here for the rare \
one that slipped through — reserve a withhold for a real shift in sense you could \
name to the author in one sentence, not for a theoretical reading nobody would \
take. Withholding a routine fix is not caution; it buries the one that matters \
under questions the author has to dismiss."""


MEANING = JudgeSpec(
    key="meaning",
    label="meaning check",
    question="does the corrected sentence still mean what the original meant?",
    prompt=_MEANING_PROMPT,
    withhold_note="This correction may change the sentence's meaning.",
    unsure_note="Unable to confirm this correction keeps the sentence's meaning.",
)


# --- the fix judge -----------------------------------------------------------

_FIX_PROMPT = """\
You are a senior proofreader checking corrections before they reach an author's \
manuscript. Earlier reviewers decided each flagged passage needed fixing, and a \
separate pass is checking whether the corrected sentence still means what the \
author meant. You are doing neither. You are asking one question, and only this \
one:

    Is this replacement the CORRECT fix?

For each change you get the paragraph it sits in, the sentence as the author \
wrote it, and the sentence as it would read after the change. Assume something \
there was worth correcting; judge the repair, not the diagnosis.

Keep the change when the corrected sentence is right: the replacement is the \
form English actually calls for here, and the sentence reads correctly with it \
in place.

Withhold it when the fix itself is wrong. That includes: a homophone corrected \
in the wrong direction ("their" to "there" where "they're" was wanted); a verb \
in the wrong form or tense for its subject; a plural or possessive formed wrongly \
("its"/"it's", "authors'"/"author's" the wrong way round); an agreement error the \
fix creates rather than removes; a misspelling replaced with a different \
misspelling, or with a real word that is not the intended one; punctuation that \
is still wrong after the change, or newly wrong because of it (a semicolon where \
the clauses will not carry one, a comma splice left spliced, a quotation mark \
closed the wrong way); a word left doubled, dropped, or stranded by the edit; \
capitalization the correction gets backwards.

Read the corrected sentence as a whole before answering. The commonest wrong fix \
is not obviously wrong at the point of the edit — it is a repair that leaves the \
rest of the sentence no longer agreeing with it.

Do not withhold a change for being inelegant, unnecessary, or not the repair you \
would have chosen. Several fixes are often defensible; you are asking whether \
THIS one is correct, not whether it is best. A blunt but correct fix is a keep.

House style is not your call. Spacing around ellipses and em dashes, serial \
commas, capitalization conventions, how numbers and dialogue punctuation are \
set — those are settled elsewhere, and applying one of them is correct here by \
definition. Never withhold a house-style change.

Nor is voice your call. Where a change touches dialect, a coined name, or a \
deliberate stylization, whether it should have been made at all is another pass's \
question — if the replacement is technically well formed, keep it.

Calibrate accordingly. These are the corrections of an ordinary proofread and the \
overwhelming majority of them are correct. You are here for the rare fix that is \
itself an error — reserve a withhold for one you could show the author is wrong \
in a sentence, not for a fix you merely dislike."""


FIX = JudgeSpec(
    key="fix",
    label="fix check",
    question="is this replacement the correct fix?",
    prompt=_FIX_PROMPT,
    withhold_note="This correction may not be the right fix.",
    unsure_note="Unable to confirm this correction is the right fix.",
)


SPECS: dict[str, JudgeSpec] = {s.key: s for s in (MEANING, FIX)}


def default_prompt(key: str) -> str:
    """The built-in instructions for one judge — for the review panel to pre-fill
    its editable field, and to fall back to when that field is cleared."""
    spec = SPECS.get(key)
    return spec.prompt if spec else ""


# --- the report --------------------------------------------------------------

@dataclass(frozen=True)
class JudgeReport:
    """What one gate did, for the caller and the run report.

    `withheld` are the findings the judge would not vouch for, already rewritten
    with the margin note the author will read; `positions` are where each sat in
    the list handed to `screen`, so the caller can put it back exactly. Position
    rather than finding_id on purpose — see `_Verdict.item`.

    `answered` counts the changes that actually came back with a verdict. It is
    reported rather than inferred because these passes fail open: a judge that
    refused every call withholds nothing, which is indistinguishable from a judge
    that read everything and approved it — unless the run says which."""
    spec: JudgeSpec
    withheld: list[Finding]
    positions: tuple[int, ...]
    checked: int
    calls: int
    answered: int = 0

    @property
    def n_withheld(self) -> int:
        return len(self.withheld)

    @property
    def unread(self) -> int:
        """Changes that went out with no verdict on them — applied unread."""
        return max(0, self.checked - self.answered)


# --- rendering ---------------------------------------------------------------

def _change_view(f: Finding, para_text: str) -> tuple[str, str]:
    """The sentence the change sits in, before and after it.

    Every finding that reaches a gate has already been validated, so it carries
    the exact minimal edit as an anchor — which is better to render from than the
    finding's own quote. Some sources (the rewrite pass especially) quote a whole
    paragraph, and the paragraph is already at the top of the prompt: repeating it
    twice per item would triple the tokens and bury a two-character fix inside two
    walls of identical prose. Falls back to the finding's own quote when there is
    no anchor to cut from."""
    a = f.anchor
    if a is None or para_text[a.start:a.end] != a.delete_text:
        return f.original_text, f.corrected_text
    from .sweeps import sentence_window
    window, lo, _ = sentence_window(para_text, a.start, a.end)
    after = window[:a.start - lo] + a.insert_text + window[a.end - lo:]
    return window, after


class Judge:
    """One paragraph's changes in, verdicts out. `fetch` is pure — no shared
    state, no usage bookkeeping — so paragraphs judge concurrently and the caller
    folds the results in serially."""

    def __init__(self, spec: JudgeSpec, provider: Provider, model: str, *,
                 instructions: str = "", context: str = "",
                 max_tokens: int = 4000):
        self.spec = spec
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        parts = [instructions.strip() or spec.prompt, _PROTOCOL]
        if context:
            parts.append(context)
        self.system_prompt = "\n\n".join(parts)
        self.schema = strict_json_schema(_Verdicts)

    def _render(self, para_text: str, findings: Sequence[Finding]) -> str:
        lines = [f"PARAGRAPH:\n{para_text}\n", "PROPOSED CHANGES:"]
        for n, f in enumerate(findings, start=1):
            # Numbered, and deliberately bare: the error type and the rule that
            # produced the change are withheld, because naming them invites the
            # judge to re-litigate whether the change should have been made at
            # all — which earlier passes already settled — instead of answering
            # the one question asked here.
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
        silently withhold or drop a correction. An item number outside the list
        it was given is dropped rather than wrapped around."""
        if result.stop_reason != "ok" or result.parsed is None:
            log.error("%s: no usable answer for %d change(s): %s",
                      self.spec.label, len(findings),
                      result.error or result.stop_reason)
            return {}
        try:
            parsed = _Verdicts.model_validate(result.parsed)
        except ValidationError as e:
            log.error("%s: answer did not match the schema: %s",
                      self.spec.label, e)
            return {}
        return {v.item - 1: v.model_dump()
                for v in parsed.verdicts if 1 <= v.item <= len(findings)}


def _margin_note(f: Finding, reason: str) -> str:
    """What the author reads beside a withheld change: the correction that was
    proposed, then why it was not made. The proposal comes from the finding's own
    explanation where it has one — that is the sentence the pass that proposed it
    would have shown — and falls back to naming the change itself."""
    proposed = (f.explanation or "").strip().rstrip(".")
    if not proposed:
        proposed = "A correction was proposed here"
    return f"{proposed}. Not applied: {reason}"


def screen(findings: Sequence[Finding], para_text: dict[str, str],
           provider: Provider, *, spec: JudgeSpec, model: str,
           instructions: str = "", context: str = "", max_tokens: int = 4000,
           concurrency: int = 8, flag_unsure: bool = True,
           usage: Usage) -> JudgeReport:
    """Put every change in `findings` to one judge and return the ones it will
    not vouch for.

    The caller passes only changes that would actually become tracked edits — a
    question changes no text, so there is nothing to judge, and a formatting mark
    changes no characters at all. Each returned finding carries the margin note
    the author will read; the caller withdraws it in place.

    `flag_unsure` treats an "unsure" verdict as a withhold, which is the default
    and the safe reading: these gates exist to stop a silent change, and a judge
    that cannot vouch for one has not vouched for it."""
    from concurrent.futures import ThreadPoolExecutor

    # Findings are grouped by paragraph but tracked by position, so a verdict
    # always lands back on the finding that was judged.
    by_para: dict[str, list[tuple[int, Finding]]] = {}
    for i, f in enumerate(findings):
        if f.para_id in para_text:              # no paragraph -> nothing to read
            by_para.setdefault(f.para_id, []).append((i, f))
    if not by_para:
        return JudgeReport(spec, [], (), 0, 0)

    judge = Judge(spec, provider, model, instructions=instructions,
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
            usage.add(result.usage, model=model)
            for offset, v in judge.parse(result, [f for _, f in items]).items():
                verdicts[items[offset][0]] = v

    flagged = {"withhold"} | ({"unsure"} if flag_unsure else set())
    withheld: list[Finding] = []
    positions: list[int] = []
    for pos, f in enumerate(findings):
        v = verdicts.get(pos)
        if v is None or v["verdict"] not in flagged:   # fail open on no answer
            continue
        reason = (v.get("reason") or "").strip()
        note = reason or (spec.withhold_note if v["verdict"] == "withhold"
                          else spec.unsure_note)
        # The margin comment has to answer two questions, because it is all the
        # author gets: what was going to be changed here, and why it wasn't.
        withheld.append(replace(f, force_query=True,
                                explanation=_margin_note(f, note)))
        positions.append(pos)

    checked = sum(len(items) for items in by_para.values())
    if len(verdicts) < checked:
        # Fail-open means those changes were applied unread. Say so loudly here
        # and in the run report — a silent gate reads exactly like a clean one.
        log.warning("%s: only %d of %d change(s) got a verdict; the other %d "
                    "were applied unread.", spec.label, len(verdicts), checked,
                    checked - len(verdicts))
    if withheld:
        log.info("%s: %d of %d change(s) held back as questions (%d paragraph "
                 "call(s)).", spec.label, len(withheld), checked, len(by_para))
    else:
        log.info("%s: all %d read change(s) passed (%d paragraph call(s)).",
                 spec.label, len(verdicts), len(by_para))
    return JudgeReport(spec, withheld, tuple(positions), checked, len(by_para),
                       answered=len(verdicts))
