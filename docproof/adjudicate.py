"""Candidate-adjudication pass: find suspected real-word typos deterministically,
let the model rule on each one in context.

The plain model passes miss letter-level typos for a structural reason, not a
reasoning one: in a 2,500-token chunk of fluent prose a single wrong word —
"I just staired at it", "the best swordsmen in the city" — reads right, and the
model (like a human) glides over it. Effort does not help; noticing a needle is
not a thinking-harder problem. And a membership-only dictionary cannot help
either: "staired" is not a word, but "swordsmen" is — just the wrong one here.

So this module inverts the problem. It generates *candidates* by cheap local
signals — an edit-distance-1 neighbour that is far more common than the word as
written, or a manuscript word the spell scan protected that sits one edit from a
common word — and hands each to the model as a single, focused question: is this
word right here, or a typo for that one? Models are near-perfect at a presented
choice. Nothing is corrected on frequency alone; the generator only proposes,
and the model rules in context, so a deliberate coinage survives.

The output is ordinary Findings, so they flow through the same validator, anchor
and channel machinery as every other pass: a confident fix becomes a tracked
change, an uncertain one a margin query.
"""
from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Sequence

from pydantic import BaseModel, Field

from .models import Finding, ParagraphRef, Usage
from .providers import Provider
from .providers.base import strict_json_schema
from .spellscan import _WORD, _dictionary
from .windowing import WindowReport, log_report, resolve_window

log = logging.getLogger("docproof.adjudicate")

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


@lru_cache(maxsize=1)
def _zipf():
    """The frequency function, imported lazily so a run that never adjudicates
    (the pass is off, or nothing survives generation) pays nothing for the
    import and its data load."""
    from wordfreq import zipf_frequency
    return zipf_frequency


def zipf(word: str) -> float:
    return _zipf()(word.lower(), "en")


def edits1(word: str) -> set[str]:
    """Every string one edit (delete/transpose/replace/insert) from `word`,
    lowercased. Norvig's classic set; small enough to enumerate per word."""
    w = word.lower()
    splits = [(w[:i], w[i:]) for i in range(len(w) + 1)]
    deletes = [a + b[1:] for a, b in splits if b]
    transposes = [a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1]
    replaces = [a + c + b[1:] for a, b in splits if b for c in _ALPHABET]
    inserts = [a + c + b for a, b in splits for c in _ALPHABET]
    return set(deletes + transposes + replaces + inserts) - {w}


@dataclass(frozen=True)
class Candidate:
    """One suspected typo, sited at a specific occurrence so the fix is exact."""
    para_id: str
    word: str            # the suspect word, exactly as written in the manuscript
    start: int           # character offset of `word` in the paragraph text
    end: int
    suggestion: str      # the proposed correction, matched to `word`'s casing
    kind: str            # "typo" | "real_word" | "near_miss" | "denylist"


def _match_case(source: str, target: str) -> str:
    """Cast the suggestion into the source word's casing, so replacing
    "Staired" gives "Stared", not "stared"."""
    if source.isupper():
        return target.upper()
    if source[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def _known(dic, word: str) -> bool:
    return bool(dic.lookup(word) or dic.lookup(word.lower())
               or dic.lookup(word.capitalize())) if dic else zipf(word) > 0


def _best_neighbour(word: str, dic, *, min_len: int) -> tuple[str | None, float]:
    """The most common real-word neighbour one edit away, and its frequency."""
    best, best_z = None, -1.0
    wl = word.lower()
    for n in edits1(word):
        if len(n) < min_len or n == wl:
            continue
        nz = zipf(n)
        if nz > best_z and _known(dic, n):
            best, best_z = n, nz
    return best, best_z


def generate(paragraphs: Sequence[ParagraphRef], *,
             protected: Sequence[str] = (),
             denylist: Mapping[str, str] | None = None,
             dictionary: str = "en_US",
             near_miss_gap: float = 2.5,
             min_len: int = 4,
             max_candidates: int = 500) -> list[Candidate]:
    """Deterministic candidate sites, from three high-precision signals only:

      * typo — the word is not in the dictionary at all and a real word sits one
        edit away (staired -> stared, farer -> fairer). The dictionary already
        knows these are non-words; this just pairs each with its likeliest twin
        and, unlike the passive "words to look at" list, makes the model rule.
      * near_miss — a word the spell scan PROTECTED as the author's own, but
        which sits one edit from a common word by a wide margin (Annastasia ->
        Anastasia). Protection is meant for coinages, not for a misspelling the
        author happens to repeat.
      * denylist — a spelling the house never accepts (alot, aswell). The fix is
        supplied directly, because the right form is often two words that no
        edit-distance search would reach, and it fires however the word is cased.

    A broad "valid word with a commoner neighbour" signal was tried and dropped:
    on a real manuscript it flagged hundreds of correct literary words (glowered
    -> lowered, roiling -> rolling) indistinguishable by frequency from the true
    errors (swordsmen -> swordsman), so it could only add noise and — since the
    model occasionally errs — risk to correct prose. See the module tests.

    `protected` is the spell scan's lexicon. A protected word surfaces only
    through near_miss, and only when a common word sits within near_miss_gap
    zipf points — a genuine coinage has no common twin. `denylist` overrides
    both protection and the dictionary: a denylisted word is always ruled on.
    """
    dic = _dictionary(dictionary)
    protected_l = {w.lower() for w in protected}
    deny = {k.lower().strip(): v for k, v in (denylist or {}).items()}

    # One decision per distinct surface spelling, then applied to each of its
    # occurrences: a typo repeated through the book is one judgement, many fixes.
    forms: dict[str, list[tuple[str, int, int]]] = {}
    for p in paragraphs:
        for m in _WORD.finditer(p.text):
            w = m.group(0)
            if len(w) < min_len or not w.isalpha():
                continue
            forms.setdefault(w.lower(), []).append((p.para_id, m.start(), m.end()))

    verdicts: dict[str, tuple[str, str] | None] = {}   # lower -> (suggestion, kind)
    for wl in forms:
        # The house denylist first, and unconditionally: it carries its own fix,
        # so no dictionary lookup or neighbour search can talk it out of ruling.
        if wl in deny:
            verdicts[wl] = (deny[wl], "denylist")
            continue
        is_protected = wl in protected_l
        known = _known(dic, wl)
        # Only unknown words, or protected ones, are ever candidates: a word the
        # dictionary accepts and the author did not coin is left entirely alone.
        if known and not is_protected:
            verdicts[wl] = None
            continue
        best, best_z = _best_neighbour(wl, dic, min_len=min_len)
        verdict = None
        if best is not None:
            if is_protected:
                # A protected coinage only yields if a common word is very close.
                if best_z - max(zipf(wl), 0.0) >= near_miss_gap and best_z >= 3.0:
                    verdict = (best, "near_miss")
            elif not known:
                verdict = (best, "typo")
        verdicts[wl] = verdict

    cands: list[Candidate] = []
    for wl, verdict in verdicts.items():
        if verdict is None:
            continue
        suggestion, kind = verdict
        for para_id, start, end in forms[wl]:
            cands.append(Candidate(
                para_id=para_id, word=wl, start=start, end=end,
                suggestion=suggestion, kind=kind))
    # Stable order (document position), then cap to bound cost. A run that trips
    # the cap logs it, so a silently-truncated sweep never reads as "all clear".
    cands.sort(key=lambda c: (c.para_id, c.start))
    if len(cands) > max_candidates:
        log.info("Adjudication: %d candidate(s) over the %d cap; taking the "
                 "first %d in document order.", len(cands), max_candidates,
                 max_candidates)
        cands = cands[:max_candidates]
    by_kind = {}
    for c in cands:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    log.info("Adjudication: %d candidate(s) to rule on (%s)", len(cands),
             ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())) or "none")
    return cands


# --- model adjudication -------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s")


def _sentence_around(text: str, start: int, end: int) -> str:
    """The one sentence containing [start, end), for context in the question.
    Falls back to the whole paragraph when boundaries are unclear."""
    left = 0
    for m in _SENTENCE_END.finditer(text, 0, start):
        left = m.end()
    rm = _SENTENCE_END.search(text, end)
    right = rm.end() if rm else len(text)
    return text[left:right].strip() or text.strip()


class _Verdict(BaseModel):
    index: int = Field(description="the item number being ruled on")
    is_error: bool = Field(
        description="true only if the flagged word is a genuine misspelling or "
        "typo in this sentence; false if it is a real word used correctly, "
        "including archaic, dialect, technical, or invented/fantasy words")
    correction: str = Field(
        default="",
        description="the corrected word, only when is_error is true; else empty")
    confidence: str = Field(
        description="high only when the correction is beyond doubt; medium or "
        "low when the word might be deliberate")


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


_SYSTEM = """\
You are a careful proofreader ruling on suspected typos. Each item gives a WORD \
flagged by a dictionary, the SENTENCE it appears in, and a possible correction \
the dictionary guessed. For each item, decide whether the flagged word is a \
genuine misspelling in that sentence.

This is a literary manuscript. Many flagged words are CORRECT and must be kept:
- Invented names, places, and coinages (a fantasy or sci-fi novel is full of them).
- Archaic, dialect, poetic, or deliberately non-standard spellings.
- Technical or rare real words (charnel, nocked, penumbral, knapped, armoire).
- A character's stylized or in-voice spelling.

The dictionary's guessed correction is often WRONG (it matches letters, not \
meaning); treat it only as a hint and supply the correct word yourself when a \
word truly is a typo. Set is_error true ONLY when the word is a real misspelling \
in context and you can give the word the author plainly meant. When in any \
doubt, set is_error false — never "correct" a word that could be deliberate. \
Reserve high confidence for cases beyond argument (staired -> stared); use \
medium or low whenever the word might be intended.\
"""


def _build_user(batch: list[tuple[int, Candidate, str]]) -> str:
    lines = []
    for n, cand, sentence in batch:
        hint = f'  possible correction: "{cand.suggestion}"' if cand.suggestion else ""
        lines.append(f'{n}. word: "{cand.word}"\n'
                     f'   sentence: {sentence}\n{hint}'.rstrip())
    return "\n\n".join(lines)


def merge_candidates(*groups: Sequence[Candidate]) -> list[Candidate]:
    """One candidate per site across sources. When the deterministic generator
    and the glossary both land on a word, the first-listed group wins the
    suggestion — callers pass the better-sourced group first."""
    seen: set[tuple[str, int, int]] = set()
    out: list[Candidate] = []
    for group in groups:
        for c in group:
            k = (c.para_id, c.start, c.end)
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
    return out


def adjudicate(candidates: Sequence[Candidate],
               paragraphs: Sequence[ParagraphRef],
               provider: Provider, *, model: str, max_tokens: int,
               usage: Usage, ids, batch_size: int = 40,
               edit_confidence: str = "high", loss_sink: list | None = None,
               concurrency: int = 1) -> list[Finding]:
    """Ask the model to rule on each candidate in context and turn confident
    misspellings into Findings. Safety is in the routing, not the detector: only
    a model-affirmed correction at `edit_confidence` becomes a tracked-change
    Finding; every other affirmed correction is force_query'd to the margin so a
    human decides, and a "keep" produces nothing. So a wrong candidate costs at
    most a query, never a silent miscorrection.

    `concurrency` windows are in flight at once. The split is the verifier's: the
    calls fan out, the answers are folded back in window order on this thread. A
    window's verdicts must land in the order they were asked for — findings claim
    their span in the order they are emitted, and the ids come off a shared
    counter — so only the waiting is parallel, never the folding. 1 (the default)
    is the old strictly-sequential behaviour, for callers with no config in hand."""
    if not candidates:
        return []
    from concurrent.futures import ThreadPoolExecutor

    text_of = {p.para_id: p.text for p in paragraphs}
    # Attach each candidate's sentence once; skip any whose paragraph vanished.
    enriched = [(c, text_of[c.para_id],
                 _sentence_around(text_of[c.para_id], c.start, c.end))
                for c in candidates if c.para_id in text_of]
    windows = [enriched[i:i + batch_size]
               for i in range(0, len(enriched), batch_size)]
    schema = strict_json_schema(_Verdicts)           # deep-copies; hoist off the pool

    def fetch(window, ceiling: int = max_tokens):
        numbered = [(n + 1, c, sent) for n, (c, _para, sent) in enumerate(window)]
        return provider.complete_structured(
            model=model, system=_SYSTEM, user=_build_user(numbered),
            schema=schema, schema_name="verdicts",
            max_tokens=ceiling)

    report = WindowReport(label="adjudication")
    findings: list[Finding] = []
    rank = {"low": 0, "medium": 1, "high": 2}
    edit_floor = rank.get(edit_confidence, 2)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(w, pool.submit(fetch, w)) for w in windows]
        try:
            for window, future in pending:
                result = future.result()
                usage.add(result.usage)              # fold serially: not thread-safe
                # Recovery (halve a truncated window, re-ask an unanswered item)
                # runs here, on this thread, so the ordering the folding depends
                # on is untouched. A candidate still unanswered afterwards is
                # counted in `report`, never mistaken for a "not an error".
                rows = resolve_window(
                    window, result, fetch=fetch, rows_of=_rows_of,
                    max_tokens=max_tokens, report=report, usage_sink=usage.add)
                findings.extend(
                    _findings_from(rows, window, ids, rank, edit_floor))
        except BaseException:
            # Every window was queued up front, and the pool drains its queue on
            # the way out — so without this an abort keeps buying the rest of the
            # book. The serial loop it replaced stopped at the first failure, and
            # so must this. Only calls not yet started can be recalled.
            for _w, unstarted in pending:
                unstarted.cancel()
            raise
    log_report(report)
    if loss_sink is not None:
        loss_sink.append(report)
    log.info("Adjudication: %d correction(s) from %d candidate(s)%s",
             len(findings), len(candidates),
             f" — {report.lost} UNRULED" if report.lost else "")
    return findings


def _rows_of(parsed: dict, items) -> dict[int, "_Verdict"]:
    """A parsed body as {1-based item number: verdict}, or nothing at all when it
    does not validate — the caller then treats those items as unanswered and
    re-asks, rather than counting them as ruled."""
    try:
        return {v.index: v for v in _Verdicts.model_validate(parsed).verdicts}
    except Exception as e:                           # malformed structured output
        log.error("adjudication: response did not match the schema: %s", e)
        return {}


def _findings_from(rows: dict, window, ids, rank: dict,
                   edit_floor: int) -> list[Finding]:
    """One window's verdicts as Findings, in the order they were asked.

    `rows` maps a 0-based offset in `window` to that item's verdict and carries
    only what actually came back; a candidate missing from it was never ruled on
    and yields nothing, which is what `adjudicate`'s WindowReport counts.

    Split out of `adjudicate` only so the folding stays a plain serial routine
    while the calls around it run on a pool: everything here touches shared
    state (the id counter) and must not be reached from a worker thread."""
    findings: list[Finding] = []
    for offset in sorted(rows):
        v = rows[offset]
        cand, para_text, _sentence = window[offset]
        if not v.is_error or not v.correction.strip():
            continue
        correction = _match_case(cand.word, v.correction.strip())
        if correction.lower() == cand.word.lower():
            continue                                 # a no-op "fix"
        corrected_para = (para_text[:cand.start] + correction
                          + para_text[cand.end:])
        conf = v.confidence if v.confidence in rank else "low"
        findings.append(Finding(
            finding_id=f"a-{next(ids):04d}",
            chunk_id="adjudicate",
            para_id=cand.para_id,
            error_type="spelling",
            original_text=para_text,
            occurrence=1,
            corrected_text=corrected_para,
            explanation=f'"{cand.word}" appears to be a misspelling of '
                        f'"{correction}".',
            confidence=conf,
            # Only a high-confidence call is trusted as a silent edit; a
            # softer one asks rather than changes.
            force_query=rank[conf] < edit_floor))
    return findings
