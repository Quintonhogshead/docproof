"""Rewrite-then-diff pass: reshape detection into generation.

The plain detector passes ask the model to FIND errors, which fights its own
fluency prior — a missing word or a wrong-but-real word reads right and the
model glides over it (the same reason a human does). This pass inverts that:
it asks the model to RETYPE each paragraph, correcting only objective mechanical
errors and changing nothing else, then deterministically diffs the rewrite
against the source. Generation rides the prior that detection fights, so it
surfaces the missing-word / letter-typo / agreement misses the detector leaves.

Every diff is only a *candidate*. Precision is in the routing, exactly as in the
adjudication pass: a second, skeptical pass rules on each proposed edit in
context, and only an affirmed error at `edit_confidence` becomes a tracked
change — a softer call is a margin query, a "keep" is nothing. So a rewrite that
over-corrects intentional voice (a character's dialect, a stylized spelling)
costs at most a question, never a silent miscorrection. Whole-document only.

Measured on Johnson Book 1: locates ~25% of the core-mechanical errors the
detector currently misses, at a ~3-4% raw over-correction rate on adversarial
clean traps that the confirm step is here to filter.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Sequence

from pydantic import BaseModel, Field

from .agreement import canonical_anchors
from .models import Chunk, Finding, ParagraphRef, Usage
from .providers import Provider
from .providers.base import strict_json_schema
from .windowing import WindowReport, log_report, resolve_window

log = logging.getLogger("docproof.rewrite")

_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class RewriteCandidate:
    """One diff between a paragraph and its minimal-edit rewrite, sited exactly."""
    para_id: str
    start: int
    end: int
    original: str        # paragraph.text[start:end]
    replacement: str     # what the rewrite put there
    note: str | None = None  # a ready-made margin explanation, if the proposing
                             # source has a better one than the generic default
                             # (Sapling's describe() line); None uses _explanation
    # The repair channel tags every member of one sentence's repair with a shared
    # non-empty cluster_id so the members stay atomic downstream (see
    # docproof/repair.py and Finding.cluster_id). Empty for the single-edit
    # sources that share this candidate type (rewrite, LanguageTool, Sapling,
    # chapter sweep), whose diffs are independent, not co-dependent.
    cluster_id: str = ""


# --- propose: rewrite each paragraph, diff against the source ------------------

PROPOSE_SYSTEM = """\
You are a meticulous copy editor performing a MINIMAL-EDIT proofread of a novel.
You will be given numbered paragraphs. For EACH, return the SAME paragraph with
only objective, unambiguous mechanical errors corrected and NOTHING else changed.

FIX (only when unambiguous):
- misspellings and typos, including a real word that is the wrong word
- a missing, doubled, or wrong word
- subject-verb and pronoun agreement
- homophone errors (their/there, its/it's, bared/barred)
- missing or clearly wrong punctuation required by grammar
- capitalization of proper nouns and sentence starts

DO NOT:
- rephrase, reorder, restructure, or tighten wording
- change word choice for style, or 'improve' anything that is not an error
- add or delete content, or alter voice, dialect, or deliberate fragments
- change dialogue in a way that recharacterizes a speaker

If a paragraph contains no objective error, return it EXACTLY as given, character
for character. Preserve all original spacing and quotation marks. Return every
paragraph by its given id, in the `corrected` field."""


# Prompt-diverse sampling: when several retypes are taken (rewrite.samples > 1),
# each looks hardest for a different class of error rather than asking the same
# question twice. They share the same FIX/DON'T contract — only the emphasis
# differs — so precision is unchanged and the union covers more. Lens 0 is the
# plain pass, so a single-sample run is byte-for-byte the original prompt.
_LENS_FOCUS = [
    "",
    "\n\nFOCUS especially on small, easily-skipped omissions — the errors that "
    "read as correct at a glance: a missing \"a\"/\"an\"/\"the\"/\"of\"/\"to\", a "
    "dropped plural -s, a missing -ly on an adverb, a letter left out of a word "
    "or a name, a missing \"it\"/\"is\"/\"not\". Look for them deliberately.",
    "\n\nFOCUS especially on agreement and word form: subject-verb agreement, "
    "pronoun case (\"between her and me\", not \"her and I\"), "
    "pronoun-antecedent agreement, verb-tense consistency, and the correct "
    "preposition for the idiom. Flag only a clear error, never a style choice.",
]


def lens_system(sample: int) -> str:
    """The propose system prompt for one sample. Lens 0 is the plain prompt;
    later samples add a focus, cycling if there are more samples than lenses."""
    return PROPOSE_SYSTEM + _LENS_FOCUS[sample % len(_LENS_FOCUS)]


class _Para(BaseModel):
    id: str = Field(description="the paragraph id exactly as given")
    corrected: str = Field(description="the minimally-corrected paragraph")


class _Rewritten(BaseModel):
    paragraphs: list[_Para]


def render(paras: Sequence[ParagraphRef]) -> str:
    """The user message for a chunk: each paragraph tagged by its id, so the
    model returns corrections keyed the same way."""
    return "\n\n".join(f"[{p.para_id}]\n{p.text}" for p in paras)


def rewrite_schema() -> dict:
    return strict_json_schema(_Rewritten)


def _diff_candidates(para: ParagraphRef, corrected: str, *,
                     max_add: int, max_span: int) -> list[RewriteCandidate]:
    if not corrected or corrected == para.text:
        return []
    out: list[RewriteCandidate] = []
    for a in canonical_anchors(para.text, corrected):
        if not (a.delete_text.strip() or a.insert_text.strip()):
            continue                        # pure whitespace (dropped trailing space)
        if len(a.delete_text) > max_span or len(a.insert_text) > max_span:
            continue                        # a paraphrase, not a minimal fix
        if len(a.insert_text) - len(a.delete_text) > max_add:
            continue
        out.append(RewriteCandidate(
            para_id=para.para_id, start=a.start, end=a.end,
            original=a.delete_text, replacement=a.insert_text))
    return out


def candidates_from_result(chunk: Chunk, parsed: dict | None, *,
                           max_add: int = 24, max_span: int = 48,
                           report: WindowReport | None = None
                           ) -> list[RewriteCandidate]:
    """Turn one chunk's rewrite response (a `_Rewritten` payload, however it was
    obtained — a live call or a batch result) into size-guarded candidates.

    `report`, if given, counts how many of the chunk's paragraphs actually came
    back retyped. The batch collector cannot split and re-ask the way `propose`
    does — the requests were bought hours ago — so counting is all it can do, and
    it is the difference between a truncated retype reading as a paragraph with
    nothing to fix and reading as a paragraph nobody looked at."""
    if report is not None:
        report.asked += len(chunk.paragraphs)
    if not parsed:
        return []
    try:
        obj = _Rewritten.model_validate(parsed)
    except Exception as e:
        log.warning("rewrite: chunk %s bad response: %s", chunk.chunk_id, e)
        return []
    by_id = {p.para_id: p for p in chunk.paragraphs}
    cands: list[RewriteCandidate] = []
    seen = 0
    for pr in obj.paragraphs:
        para = by_id.get(pr.id)
        if para is not None:
            seen += 1
            cands.extend(_diff_candidates(
                para, pr.corrected, max_add=max_add, max_span=max_span))
    if report is not None:
        report.answered += seen
    return cands


def dedup_candidates(cands: Sequence[RewriteCandidate]) -> list[RewriteCandidate]:
    """The union of candidates from independent retypes: an exact duplicate (same
    span AND replacement, from two samples that caught the same error) collapses
    to one, while two different fixes for the same span are both kept — the
    confirm pass rules on each and the validator keeps the first to claim it. So
    ensembling only ever adds coverage, never a contradiction the router can't
    settle."""
    seen: set[tuple[str, int, int, str]] = set()
    out: list[RewriteCandidate] = []
    for c in cands:
        key = (c.para_id, c.start, c.end, c.replacement)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def propose(chunks: Sequence[Chunk], provider: Provider, *, model: str,
            max_tokens: int, usage: Usage, max_add: int = 24,
            max_span: int = 48, workers: int = 8, samples: int = 1,
            diverse: bool = True,
            loss_sink: list | None = None) -> list[RewriteCandidate]:
    """Rewrite every paragraph live and return the size-guarded diffs as
    candidates. With `samples` > 1 each chunk is retyped that many times and the
    diffs are unioned — a stochastic retype catches overlapping but not identical
    errors, so the union lifts recall. With `diverse`, each sample looks hardest
    for a different error class (see lens_system); without it they share the plain
    prompt. The synchronous path; batch review builds the same requests into its
    batch and calls `candidates_from_result` at collect. Calls run concurrently.

    Unlike the verdict passes, this one's output is proportional to its INPUT —
    it retypes every paragraph it is given — so a chunk that truncates is
    genuinely too big for the ceiling, and halving it is both the correct remedy
    and an effective one. A paragraph that still never comes back retyped is
    counted in `loss_sink`: it generated no candidates, which is indistinguishable
    from a paragraph the model read and found nothing wrong with.
    """
    schema = rewrite_schema()

    def do(job: tuple[Chunk, int]):
        """Returns (candidates, [usage], report). Usage and the loss report both
        come back rather than being folded here: this runs on the pool, and both
        Usage.add and the report's counters are lock-free read-modify-write, so
        touching either from N workers silently loses updates — under-reporting
        what the run cost, and what it failed to read."""
        chunk, sample = job
        report = WindowReport(label="rewrite retype")
        if not chunk.paragraphs:
            return [], [], report
        system = lens_system(sample) if diverse else PROPOSE_SYSTEM
        spent: list = []

        def fetch(paragraphs, ceiling: int = max_tokens):
            res = provider.complete_structured(
                model=model, system=system, user=render(paragraphs),
                schema=schema, schema_name="rewritten", max_tokens=ceiling)
            spent.append(res.usage)          # every call, including the first
            return res

        rows = resolve_window(chunk.paragraphs, fetch(chunk.paragraphs),
                              fetch=fetch, rows_of=_retype_rows,
                              max_tokens=max_tokens, report=report)
        if report.lost:
            log.warning("rewrite: chunk %s — %d paragraph(s) were never "
                        "retyped, so they produced no candidates.",
                        chunk.chunk_id, report.lost)
        return (_candidates_from_rows(chunk.paragraphs, rows, max_add=max_add,
                                      max_span=max_span), spent, report)

    jobs = [(chunk, s) for s in range(max(1, samples)) for chunk in chunks]
    cands: list[RewriteCandidate] = []
    total = WindowReport(label="rewrite retype")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for group, spent, report in ex.map(do, jobs):
            for u in spent:
                usage.add(u, model=model)            # fold serially: not thread-safe
            total.merge(report)
            cands.extend(group)
    log_report(total)
    if loss_sink is not None:
        loss_sink.append(total)
    cands = dedup_candidates(cands)
    log.info("Rewrite: %d candidate diff(s) from %d chunk(s) x %d sample(s)%s",
             len(cands), len(chunks), max(1, samples),
             f" — {total.lost} paragraph retype(s) LOST" if total.lost else "")
    return cands


def _retype_rows(parsed: dict, items) -> dict[int, "_Para"]:
    """A retype response as {1-based position in `items`: rewritten paragraph}.

    The wire protocol keys by para_id rather than by item number, so the mapping
    needs the list that was actually asked about — which changes every time a
    window is halved. A paragraph absent from the response is left unanswered
    and re-asked rather than being taken for "nothing to change here"."""
    try:
        obj = _Rewritten.model_validate(parsed)
    except Exception as e:
        log.warning("rewrite: bad response: %s", e)
        return {}
    at = {p.para_id: i for i, p in enumerate(items, 1)}
    return {at[pr.id]: pr for pr in obj.paragraphs if pr.id in at}


def _candidates_from_rows(paragraphs, rows: dict, *, max_add: int,
                          max_span: int) -> list[RewriteCandidate]:
    """Size-guarded diffs for the paragraphs that actually came back retyped."""
    out: list[RewriteCandidate] = []
    for offset in sorted(rows):
        out.extend(_diff_candidates(paragraphs[offset], rows[offset].corrected,
                                    max_add=max_add, max_span=max_span))
    return out


# --- confirm: rule on each proposed edit in context ---------------------------

_CONFIRM_SYSTEM = """\
You are a careful proofreader ruling on suggested edits to a novel. Each item \
gives a SENTENCE and a proposed minimal edit within it (an ORIGINAL span and the \
REPLACEMENT another editor suggested). Decide whether the original is a genuine, \
objective mechanical error that the replacement correctly fixes.

This is a literary manuscript. KEEP the original (is_error = false) when the \
suggested edit would touch anything deliberate:
- invented names, places, coinages, archaic/dialect/poetic spellings
- a character's stylized or in-voice wording (including illiterate notes)
- valid, if unusual, word choice, grammar, or punctuation
- anything that is a matter of style rather than a clear error

Set is_error true ONLY when the original is unambiguously wrong in context (a \
misspelling, a missing or wrong word, an agreement error, missing required \
punctuation) and the replacement is the right fix. When in any doubt, set \
is_error false. Reserve high confidence for corrections beyond argument; use \
medium or low whenever the original might be intended."""


class _CVerdict(BaseModel):
    index: int = Field(description="the item number being ruled on")
    is_error: bool = Field(
        description="true only if the original is a genuine objective error the "
        "replacement correctly fixes; false if it could be deliberate")
    confidence: str = Field(
        description="high only when the correction is beyond doubt; medium or "
        "low when the original might be intended")


class _CVerdicts(BaseModel):
    verdicts: list[_CVerdict]


def _sentence_around(text: str, start: int, end: int) -> str:
    from .adjudicate import _sentence_around as _s
    return _s(text, start, end)


def _describe(c: RewriteCandidate) -> str:
    if not c.original:
        return f'insert "{c.replacement}"'
    if not c.replacement:
        return f'delete "{c.original}"'
    return f'"{c.original}" -> "{c.replacement}"'


def _explanation(c: RewriteCandidate, *, applied: bool = False) -> str:
    """What rides beside the finding. Voice follows the channel (DP-009): a
    committed tracked change gets a declarative reason — hedging beside an
    applied edit reads as the tool doubting its own work — and only a margin
    question keeps the "may be / suggested" framing, because there it IS a
    suggestion."""
    if c.note:
        return c.note
    if applied:
        if not c.original:
            return f'Missing text: "{c.replacement}" inserted.'
        if not c.replacement:
            return f'"{c.original}" deleted as extraneous.'
        return f'"{c.original}" corrected to "{c.replacement}".'
    if not c.original:
        return f'Possible missing text — suggested: insert "{c.replacement}".'
    if not c.replacement:
        return f'Possibly delete "{c.original}".'
    return f'"{c.original}" may be an error — suggested: "{c.replacement}".'


def confirm(candidates: Sequence[RewriteCandidate],
            paragraphs: Sequence[ParagraphRef], provider: Provider, *,
            model: str, max_tokens: int, usage: Usage, ids,
            batch_size: int = 40, edit_confidence: str = "high",
            reject_sink: list | None = None, loss_sink: list | None = None,
            error_type: str = "rewrite", chunk_id: str = "rewrite",
            id_prefix: str = "r", silent: bool = False,
            concurrency: int = 1, system: str | None = None,
            mode: str = "correction") -> list[Finding]:
    """Skeptically rule on each candidate in context and turn the affirmed ones
    into Findings. Only an error affirmed at `edit_confidence` becomes a tracked
    change; a softer affirmation is force_query'd to the margin, a "keep" yields
    nothing — the same routing that makes the adjudication pass safe.

    `system` replaces the built-in confirm prompt for a source that is not ruling
    on mechanical errors. Any context a caller wants the judge to have (a story
    sheet, the conventions, a vocabulary) belongs folded into that string: this
    valve's user message is the numbered items and nothing else, and keeping it
    that way is what lets every source share one batching path.

    `mode` picks what an affirmed verdict MEANS.

    - "correction" (the default, and every mechanical caller): the candidate is
      an error, the verdict decides whether it is sure enough to edit, and the
      finding quotes the whole paragraph so the validator can shrink it back to
      the minimal diff.
    - "suggestion": the candidate is a matter of taste, so an affirmation is
      never an edit. The finding is force_query'd unconditionally — NOT via
      `edit_confidence`, which cannot express this (an unrecognised value there
      falls back to "high" and would let a confident verdict through as a
      tracked change) — and it quotes the SENTENCE rather than the paragraph,
      because that is the span the margin comment will cover and a question
      about a whole paragraph tells the author nothing about where to look.

    `concurrency` windows are in flight at once, the verifier's split: the calls
    fan out, the answers fold back in window order on this thread. The order is
    not incidental — findings claim their span in the order they are emitted, the
    ids come off a shared counter, and `reject_sink` is a record an eval reads
    positionally — so only the waiting is parallel. This valve is the one every
    candidate source shares (rewrite, LanguageTool, Sapling, the low-confidence
    promotion), and on a novel each of them can fill dozens of windows. 1 (the
    default) is the old strictly-sequential behaviour.

    `reject_sink`, if given, collects every candidate the confirm step KEPT (ruled
    not-an-error). These are where recall dies at the gate: a candidate the rewrite
    pass surfaced and confirm then discarded. Persisting them lets a compare say
    how many were real errors confirm wrongly rejected — the evidence for whether
    a stronger confirm model is worth its cost.

    `silent` marks every emitted Finding as a silent edit (a tracked change with
    no margin comment), for a candidate source that rides under the global
    `comments` switch — Sapling with `sapling.comments` off.

    `loss_sink`, if given, collects this pass's WindowReport — how many
    candidates actually got a verdict. A candidate whose window truncated is NOT
    a candidate the model kept: it was paid for and never ruled on, and without
    that count a pass that lost every window looks exactly like one that read
    everything and affirmed nothing. See docproof/windowing.py."""
    if not candidates:
        return []
    text_of = {p.para_id: p.text for p in paragraphs}
    enriched = [c for c in candidates if c.para_id in text_of]
    edit_floor = _RANK.get(edit_confidence, 2)
    windows = [enriched[i:i + batch_size]
               for i in range(0, len(enriched), batch_size)]
    schema = strict_json_schema(_CVerdicts)          # deep-copies; hoist off the pool

    def fetch(window, ceiling: int = max_tokens):
        lines = []
        for n, c in enumerate(window, 1):
            sent = _sentence_around(text_of[c.para_id], c.start, c.end)
            lines.append(f"{n}. sentence: {sent}\n   edit: {_describe(c)}")
        return provider.complete_structured(
            model=model, system=system or _CONFIRM_SYSTEM,
            user="\n\n".join(lines),
            schema=schema, schema_name="verdicts",
            max_tokens=ceiling)

    report = WindowReport(label=f"{error_type} confirm")
    findings: list[Finding] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(w, pool.submit(fetch, w)) for w in windows]
        try:
            for window, future in pending:
                res = future.result()
                usage.add(res.usage, model=model)    # fold serially: not thread-safe
                # Recovery (halve a truncated window, re-ask an unanswered item)
                # happens here, on this thread, so the ordering the folding
                # depends on is untouched. Anything still unanswered is counted
                # in `report` and reported below rather than passing for a
                # "not an error" verdict.
                rows = resolve_window(
                    window, res, fetch=fetch, rows_of=_rows_of,
                    max_tokens=max_tokens, report=report,
                    usage_sink=lambda ru: usage.add(ru, model=model))
                _fold(rows, window, text_of, findings, reject_sink, ids,
                      edit_floor, error_type=error_type, chunk_id=chunk_id,
                      id_prefix=id_prefix, silent=silent, mode=mode)
        except BaseException:
            # Every window was queued up front, and the pool drains its queue on
            # the way out — so without this an abort keeps buying the rest of the
            # book. The serial loop it replaced stopped at the first failure, and
            # so must this. Only calls not yet started can be recalled.
            for _w, unstarted in pending:
                unstarted.cancel()
            raise
    log_report(report)
    kept = len(reject_sink) if reject_sink is not None else 0
    log.info("Rewrite: %d correction(s) from %d candidate(s)%s%s", len(findings),
             len(candidates),
             f" ({kept} kept as not-an-error)" if reject_sink is not None else "",
             f" — {report.lost} UNRULED" if report.lost else "")
    if loss_sink is not None:
        loss_sink.append(report)
    return findings


def _rows_of(parsed: dict, items) -> dict[int, "_CVerdict"]:
    """A parsed body as {1-based item number: verdict}, or nothing at all when
    it does not validate — the caller then treats those items as unanswered and
    re-asks, rather than counting them as ruled."""
    try:
        return {v.index: v for v in _CVerdicts.model_validate(parsed).verdicts}
    except Exception as e:                           # malformed structured output
        log.error("confirm: response did not match the schema: %s", e)
        return {}


def _fold(rows: dict, window, text_of: dict, findings: list, reject_sink,
          ids, edit_floor: int, *, error_type: str, chunk_id: str,
          id_prefix: str, silent: bool, mode: str = "correction") -> None:
    """One window's verdicts, appended to `findings` in the order asked.

    `rows` maps a 0-based offset in `window` to that item's verdict, and carries
    only the items that actually came back — an offset missing from it was never
    ruled on, and is deliberately absent from both the findings and the reject
    log rather than being counted as a "not an error".

    Split out of `confirm` so the folding stays a plain serial routine while the
    calls around it run on a pool: the id counter, the findings list and the
    reject log are all shared, and none of them may be touched from a worker
    thread."""
    suggestion = mode == "suggestion"
    for offset in sorted(rows):
        v = rows[offset]
        c = window[offset]
        if not v.is_error:
            if reject_sink is not None:
                reject_sink.append({
                    "para_id": c.para_id, "start": c.start, "end": c.end,
                    "original": c.original, "replacement": c.replacement,
                    "confidence": v.confidence})
            continue
        para_text = text_of[c.para_id]
        conf = v.confidence if v.confidence in _RANK else "low"
        if suggestion:
            # Quote the sentence, not the paragraph: this finding is going to
            # the margin, and the quoted span IS the highlighted span there.
            #
            # corrected_text carries the suggested wording, and must keep
            # carrying it. The obvious-looking simplification is the one the
            # other query-only passes use — `corrected_text=original`, on the
            # reasoning that a question edits nothing (continuity.py does
            # exactly this). Here it silently LOSES DATA: the validator's
            # duplicate key for a force_query finding is
            # (para_id, start, "query", error_type, corrected_text)
            # (validator.py, the query branch), so two different suggestions
            # about one sentence would key identically and the second would be
            # dropped as rejected_duplicate — the author asked one question
            # instead of two, with nothing in any report to say so. Continuity
            # gets away with it because it never files two questions about one
            # sentence; a line editor does that routinely.
            # Pinned by tests/test_smoothing.py::
            # test_two_suggestions_on_one_sentence_stay_two_questions.
            from .sweeps import sentence_window
            quote, lo, occurrence = sentence_window(para_text, c.start, c.end)
            corrected = (quote[:c.start - lo] + c.replacement
                         + quote[c.end - lo:])
        else:
            quote, occurrence = para_text, 1
            corrected = para_text[:c.start] + c.replacement + para_text[c.end:]
        will_query = suggestion or _RANK[conf] < edit_floor
        findings.append(Finding(
            finding_id=f"{id_prefix}-{next(ids):04d}",
            chunk_id=chunk_id,
            para_id=c.para_id,
            error_type=error_type,
            original_text=quote,
            occurrence=occurrence,
            corrected_text=corrected,
            explanation=_explanation(c, applied=not will_query),
            confidence=conf,
            # Only a beyond-doubt affirmation edits; softer ones ask. A
            # suggestion never edits at all, however sure the judge was.
            force_query=will_query,
            silent=silent))
