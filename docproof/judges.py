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

Some items are shown together because their changes land in the same sentence \
and will be applied together. For each of those you get one "as it will read" — \
the sentence with EVERY one of those changes made — and then, per item, the \
single change that item is responsible for. Judge each item's own change as it \
reads inside that combined sentence, not inside a half-corrected one. In \
particular, never withhold an item for an inconsistency that another listed item \
in the same sentence removes: "2" -> "two" and "3" -> "three" are each wrong \
alone and right together, so both are keeps.

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



_MEANING_PROMPT = """\
You are a senior editor performing the LAST check before corrections reach an \
author's manuscript. Every change below has already been judged a legitimate \
correction by earlier reviewers, and another pass is checking separately whether \
each fix is technically right. You are not doing either of those. You are asking \
one question, and only this one:

    Does the corrected sentence still mean what the original meant?

Read that question as one about authorial intent, not textual identity. A \
change only reaches you because the original was judged an error, and for many \
of these the original's literal parse was never the reading the author was \
going for. The corrected sentence differing in referent, tense, or addressee \
from what the erroneous original technically said is not itself a withhold — \
that difference can be exactly what the correction was for. The test is \
whether the corrected sentence gives the reading the author was actually going \
for, not whether it matches the flawed original word-sense for word-sense.

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
passage is already in — including a present-tense slip inside otherwise \
past-tense narration, "asks" corrected to "asked" — changes the form and \
leaves the sense where it was; keep those. A comma inserted before a name, \
title, or endearment that dialogue is already being spoken to — direct \
address, as in "I know Molly" corrected to "I know, Molly," where Molly is \
plainly being spoken to and not described — is the same kind of fix: who is \
addressed does not change, only whether the punctuation admits it; keep those \
too. So is squaring a false dialogue tag: a verb of manner or expression \
(smiled, laughed, nodded, shrugged) cannot itself carry words, so closing the \
quotation before it with a period instead of a comma does not touch what was \
said — it only fixes which sentence the verb belongs to. Withhold only when \
the corrected sentence points at a different referent, a different time, or a \
different degree of certainty than the one the author was actually going for.

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


#
# The pipeline forces a coupled repair to arrive in pieces. "2 and 3" -> "two and
# three" under the spell-out rule is one thought, but ONE FINDING PER ERROR (see
# the analyzer) splits it into two findings, each correcting only its own
# numeral — so one reads "two and 3" and the other "2 and three". A judge shown
# either alone sees a sentence a house rule has just made INCONSISTENT and,
# rightly by its own lights, withholds it. Both die, and the sentence the author
# wanted — both numerals spelled out — was never on the table.
#
# A cohort is the group of changes that land in one sentence and therefore ship
# as one sentence. Rendered jointly, the judge reads "two and three" and keeps
# both. The grouping is deliberately generous: two truly independent fixes that
# merely share a sentence are cohorted too, which is harmless — judged jointly a
# good pair both keep, and the one case that needs care (one member held, one
# kept) is closed by re-judging the survivor alone, below. Missing a real couple
# is the only failure mode, so proximity across a sentence boundary links as
# well — a dialogue tag's closing-quote repunctuation and the following word's
# lowercasing are one repair the sentence splitter would otherwise cut in two.

_COHORT_GAP = 40           # chars between two edits still counted as one repair
_MAX_COHORT_ROUNDS = 4     # a safety bound; the held set grows every real round


def _anchor_span(f: Finding, para_text: str) -> tuple[int, int] | None:
    """Where f's edit sits in the paragraph, or None when it carries no anchor
    that still matches the text — then it cannot be spliced beside another and is
    judged alone, exactly as before cohorts existed."""
    a = f.anchor
    if a is None or para_text[a.start:a.end] != a.delete_text:
        return None
    return a.start, a.end


def _cohorts(findings: Sequence[Finding], para_text: str,
             active: set[int]) -> list[list[int]]:
    """Partition the `active` findings of one paragraph into ship-together
    groups, as lists of indices into `findings` (in order).

    Two findings link when their edits share a sentence, or sit within
    `_COHORT_GAP` characters of each other across a boundary. Union-find over
    those links; a finding with no usable anchor is always its own group."""
    from .sweeps import sentence_window
    idx = sorted(active)
    span: dict[int, tuple[int, int]] = {}
    win: dict[int, tuple[int, int]] = {}
    for i in idx:
        s = _anchor_span(findings[i], para_text)
        if s is not None:
            span[i] = s
            w, lo, _ = sentence_window(para_text, s[0], s[1])
            win[i] = (lo, lo + len(w))
    parent = {i: i for i in idx}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    anchored = [i for i in idx if i in span]
    for a_pos, i in enumerate(anchored):
        for j in anchored[a_pos + 1:]:
            same_sentence = not (win[i][1] <= win[j][0] or win[j][1] <= win[i][0])
            gap = max(span[i][0], span[j][0]) - min(span[i][1], span[j][1])
            if same_sentence or gap <= _COHORT_GAP:
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in idx:
        groups.setdefault(find(i), []).append(i)
    return [sorted(g) for g in groups.values()]


def _cohort_view(findings: Sequence[Finding], idxs: Sequence[int],
                 para_text: str) -> tuple[str, str]:
    """The sentence a cohort ships as: the smallest window covering every
    member's edit, before and after ALL of them are applied. Members are
    validated and non-overlapping, so they compose right-to-left cleanly."""
    from .sweeps import sentence_window
    spans = [_anchor_span(findings[i], para_text) for i in idxs]
    lo_edit = min(s[0] for s in spans)
    hi_edit = max(s[1] for s in spans)
    window, lo, _ = sentence_window(para_text, lo_edit, hi_edit)
    after = window
    for i in sorted(idxs, key=lambda k: findings[k].anchor.start, reverse=True):
        a = findings[i].anchor
        after = after[:a.start - lo] + a.insert_text + after[a.end - lo:]
    return window, after


def _own_change(f: Finding) -> str:
    """The one edit an item is responsible for, as delete -> insert, so a judge
    reading a jointly-rendered sentence knows which span is this item's to rule
    on and which belong to its companions."""
    a = f.anchor
    return f"{(a.delete_text if a else f.original_text)!r} -> " \
           f"{(a.insert_text if a else f.corrected_text)!r}"


def _meaning_trivial(f: Finding) -> bool:
    """Whether a change cannot alter what a sentence MEANS, so the meaning gate
    has nothing to weigh and should not see it.

    Two shapes qualify, both provable from the minimal diff alone:
      * punctuation / whitespace only — a comma set off a name in direct
        address, a period closing an action beat, a semicolon for a splice.
        Grouping and pause, never proposition.
      * case only — "But" -> "but" resuming interrupted dialogue, "god" -> "God"
        as the deity's name. The same letters.

    This is the meaning gate's blind spot, not the fix gate's: a comma CAN be
    the wrong repair (a splice wanting a semicolon), which is fix_check's
    question — so only the meaning gate bypasses these. Anything with a changed
    alphanumeric run (an inserted word, a swapped or reinflected verb) is NOT
    trivial and is judged as before."""
    a = f.anchor
    if a is not None:
        delete_text, insert_text = a.delete_text, a.insert_text
    else:
        # No anchor yet (a caller screening raw findings): reduce to the same
        # minimal diff the validator would, so a whole-sentence quote that
        # differs by one comma is read as the comma, not as two sentences.
        from .validator import shrink
        _pre, delete_text, insert_text = shrink(f.original_text, f.corrected_text)
    if delete_text == insert_text:
        return False
    if all(not c.isalnum() for c in delete_text + insert_text):
        return True                                 # punctuation / whitespace
    return delete_text.casefold() == insert_text.casefold()   # case only


def _is_flagged(verdict: dict | None, flag_unsure: bool) -> bool:
    """Whether a verdict withholds the change. One definition, shared by the
    per-paragraph loop and the final fold, so the two can never disagree about
    what 'held' means."""
    if verdict is None:
        return False
    v = verdict.get("verdict")
    return v == "withhold" or (flag_unsure and v == "unsure")


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

    def _render(self, para_text: str, findings: Sequence[Finding],
                active: set[int] | None = None) -> str:
        # Item numbers are GLOBAL (1-based over `findings`) so a verdict always
        # lands on the same change whether it was asked in the first round or a
        # later survivors-only one. `active` narrows which items are shown, never
        # how they are numbered.
        act = set(range(len(findings))) if active is None else set(active)
        companions: dict[int, list[int]] = {}
        joint: dict[int, tuple[str, str]] = {}
        for coh in _cohorts(findings, para_text, act):
            if len(coh) < 2:
                continue
            before, after = _cohort_view(findings, coh, para_text)
            for i in coh:
                companions[i] = [j + 1 for j in coh if j != i]
                joint[i] = (before, after)
        lines = [f"PARAGRAPH:\n{para_text}\n", "PROPOSED CHANGES:"]
        for n, f in enumerate(findings):
            if n not in act:
                continue
            # Numbered, and deliberately bare: the error type and the rule that
            # produced the change are withheld, because naming them invites the
            # judge to re-litigate whether the change should have been made at
            # all — which earlier passes already settled — instead of answering
            # the one question asked here.
            if n in joint:
                before, after = joint[n]
                mates = ", ".join(str(c) for c in companions[n])
                lines.append(
                    f"- item {n + 1}\n"
                    f"  as written: {before!r}\n"
                    f"  as it will read: {after!r}\n"
                    f"  item {n + 1} makes only this change: {_own_change(f)}\n"
                    f"  applied together with item(s): {mates}")
            else:
                before, after = _change_view(f, para_text)
                lines.append(
                    f"- item {n + 1}\n"
                    f"  as written: {before!r}\n"
                    f"  as corrected: {after!r}")
        lines.append("\nReturn a verdict for every item number above.")
        return "\n".join(lines)

    def fetch(self, para_text: str, findings: Sequence[Finding],
              active: set[int] | None = None):
        return self.provider.complete_structured(
            model=self.model, system=self.system_prompt,
            user=self._render(para_text, findings, active),
            schema=self.schema, schema_name="verdicts",
            max_tokens=self.max_tokens)

    def parse(self, result, findings: Sequence[Finding],
              active: set[int] | None = None) -> dict[int, dict]:
        """Verdicts keyed by 0-based offset into `findings`. A judge that
        refused, truncated, or returned junk yields nothing — the caller then
        leaves those changes exactly as they were, so a hiccup here can never
        silently withhold or drop a correction. An item number outside the list
        it was given — or outside `active`, when a survivors-only round asked
        about only some of it — is dropped rather than wrapped around."""
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
        act = set(range(len(findings))) if active is None else set(active)
        return {v.item - 1: v.model_dump()
                for v in parsed.verdicts
                if 1 <= v.item <= len(findings) and v.item - 1 in act}


def _judge_paragraph(judge: Judge, para_text: str, findings: Sequence[Finding],
                     flag_unsure: bool):
    """One paragraph's changes to a fixpoint. Returns (verdicts_by_index,
    results) — the verdicts keyed by offset into `findings`, and every raw
    result so the caller can bill each API call's usage serially.

    Round one judges every change inside the sentence it will ship in. A cohort
    that comes back MIXED — some kept, some held — is the one case joint
    rendering does not settle on its own: the kept member was vouched for beside
    a companion that is now being withdrawn, so it would ship into a sentence
    that no longer holds together. Those survivors are re-judged with the held
    members gone, and only those; the held set only grows, so this settles in a
    couple of rounds (and is capped regardless, failing open on whatever
    verdicts are in hand)."""
    verdicts: dict[int, dict] = {}
    results = []
    kept = set(range(len(findings)))
    to_judge = set(kept)
    for _ in range(_MAX_COHORT_ROUNDS):
        if not to_judge:
            break
        result = judge.fetch(para_text, findings, to_judge)
        results.append(result)
        verdicts.update(judge.parse(result, findings, to_judge))
        held = {i for i in to_judge if _is_flagged(verdicts.get(i), flag_unsure)}
        if not held:
            break
        kept -= held
        recheck: set[int] = set()
        for coh in _cohorts(findings, para_text, to_judge):
            survivors = [i for i in coh if i not in held]
            if survivors and any(i in held for i in coh):   # a mixed cohort
                recheck.update(survivors)
        to_judge = recheck & kept
    return verdicts, results


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
           bypass_trivial: bool = False,
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
    bypassed = 0
    for i, f in enumerate(findings):
        if bypass_trivial and _meaning_trivial(f):
            # A punctuation- or case-only change carries no proposition for the
            # meaning gate to weigh; judging it only risks a wrong hold on a
            # correct comma or capital. Kept as an edit, never sent to the judge.
            bypassed += 1
            continue
        if f.para_id in para_text:
            by_para.setdefault(f.para_id, []).append((i, f))
    if bypassed:
        log.info("%s: %d punctuation/case-only change(s) bypassed the gate "
                 "deterministically.", spec.label, bypassed)
    if not by_para:
        return JudgeReport(spec, [], (), 0, 0)

    judge = Judge(spec, provider, model, instructions=instructions,
                  context=context, max_tokens=max_tokens)
    # Verdicts come back keyed by their offset in the batch, which maps straight
    # onto the caller's positions — no finding_id anywhere in the round trip.
    verdicts: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(items, pool.submit(_judge_paragraph, judge, para_text[pid],
                                       [f for _, f in items], flag_unsure))
                   for pid, items in by_para.items()]
        for items, fut in pending:   # fold serially: usage.add is not thread-safe
            para_verdicts, results = fut.result()
            for r in results:        # a paragraph may take more than one call
                usage.add(r.usage, model=model)
            for offset, v in para_verdicts.items():
                verdicts[items[offset][0]] = v

    withheld: list[Finding] = []
    positions: list[int] = []
    for pos, f in enumerate(findings):
        v = verdicts.get(pos)
        if not _is_flagged(v, flag_unsure):            # fail open on no answer
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
