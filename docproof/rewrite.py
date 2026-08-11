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
                           max_add: int = 24, max_span: int = 48
                           ) -> list[RewriteCandidate]:
    """Turn one chunk's rewrite response (a `_Rewritten` payload, however it was
    obtained — a live call or a batch result) into size-guarded candidates."""
    if not parsed:
        return []
    try:
        obj = _Rewritten.model_validate(parsed)
    except Exception as e:
        log.warning("rewrite: chunk %s bad response: %s", chunk.chunk_id, e)
        return []
    by_id = {p.para_id: p for p in chunk.paragraphs}
    cands: list[RewriteCandidate] = []
    for pr in obj.paragraphs:
        para = by_id.get(pr.id)
        if para is not None:
            cands.extend(_diff_candidates(
                para, pr.corrected, max_add=max_add, max_span=max_span))
    return cands


def propose(chunks: Sequence[Chunk], provider: Provider, *, model: str,
            max_tokens: int, usage: Usage, max_add: int = 24,
            max_span: int = 48, workers: int = 8) -> list[RewriteCandidate]:
    """Rewrite every paragraph live (one call per chunk) and return the
    size-guarded diffs as candidates. The synchronous path; batch review builds
    the same requests into its batch and calls `candidates_from_result` at
    collect. Calls are independent, so they run concurrently."""
    schema = rewrite_schema()

    def do(chunk: Chunk):
        if not chunk.paragraphs:
            return []
        res = provider.complete_structured(
            model=model, system=PROPOSE_SYSTEM, user=render(chunk.paragraphs),
            schema=schema, schema_name="rewritten", max_tokens=max_tokens)
        usage.add(res.usage)
        if res.stop_reason != "ok" or not res.parsed:
            log.warning("rewrite: chunk %s returned %s", chunk.chunk_id,
                        res.error or res.stop_reason)
            return []
        return candidates_from_result(chunk, res.parsed, max_add=max_add,
                                      max_span=max_span)

    cands: list[RewriteCandidate] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for group in ex.map(do, chunks):
            cands.extend(group)
    log.info("Rewrite: %d candidate diff(s) from %d chunk(s)", len(cands),
             len(chunks))
    return cands


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


def _explanation(c: RewriteCandidate) -> str:
    if not c.original:
        return f'Possible missing text — suggested: insert "{c.replacement}".'
    if not c.replacement:
        return f'Possibly delete "{c.original}".'
    return f'"{c.original}" may be an error — suggested: "{c.replacement}".'


def confirm(candidates: Sequence[RewriteCandidate],
            paragraphs: Sequence[ParagraphRef], provider: Provider, *,
            model: str, max_tokens: int, usage: Usage, ids,
            batch_size: int = 40, edit_confidence: str = "high") -> list[Finding]:
    """Skeptically rule on each candidate in context and turn the affirmed ones
    into Findings. Only an error affirmed at `edit_confidence` becomes a tracked
    change; a softer affirmation is force_query'd to the margin, a "keep" yields
    nothing — the same routing that makes the adjudication pass safe."""
    if not candidates:
        return []
    text_of = {p.para_id: p.text for p in paragraphs}
    enriched = [c for c in candidates if c.para_id in text_of]
    edit_floor = _RANK.get(edit_confidence, 2)

    findings: list[Finding] = []
    for i in range(0, len(enriched), batch_size):
        window = enriched[i:i + batch_size]
        lines = []
        for n, c in enumerate(window, 1):
            sent = _sentence_around(text_of[c.para_id], c.start, c.end)
            lines.append(f"{n}. sentence: {sent}\n   edit: {_describe(c)}")
        res = provider.complete_structured(
            model=model, system=_CONFIRM_SYSTEM, user="\n\n".join(lines),
            schema=strict_json_schema(_CVerdicts), schema_name="verdicts",
            max_tokens=max_tokens)
        usage.add(res.usage)
        if res.stop_reason != "ok" or res.parsed is None:
            log.error("rewrite-confirm batch %d: %s", i // batch_size,
                      res.error or res.stop_reason)
            continue
        try:
            parsed = _CVerdicts.model_validate(res.parsed)
        except Exception as e:
            log.error("rewrite-confirm batch %d: bad response: %s",
                      i // batch_size, e)
            continue
        for v in parsed.verdicts:
            if not (1 <= v.index <= len(window)):
                continue
            c = window[v.index - 1]
            if not v.is_error:
                continue
            para_text = text_of[c.para_id]
            conf = v.confidence if v.confidence in _RANK else "low"
            findings.append(Finding(
                finding_id=f"r-{next(ids):04d}",
                chunk_id="rewrite",
                para_id=c.para_id,
                error_type="rewrite",
                original_text=para_text,
                occurrence=1,
                corrected_text=para_text[:c.start] + c.replacement
                + para_text[c.end:],
                explanation=_explanation(c),
                confidence=conf,
                # Only a beyond-doubt affirmation edits; softer ones ask.
                force_query=_RANK[conf] < edit_floor))
    log.info("Rewrite: %d correction(s) from %d candidate(s)", len(findings),
             len(candidates))
    return findings
