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
from .sweeps import sentence_window
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
             respell: Mapping[str, str] | None = None,
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

    `respell` maps spellings that are valid English but the wrong VARIANT for
    this manuscript (grey in a U.S. book) to the variant's own form. Like the
    denylist it is always ruled on — the dictionary accepts these words, so
    nothing else would ever raise them — but it gets its own kind, because
    the question the model must answer is different: not "is this a typo" but
    "is this ordinary prose, or a proper name that keeps its spelling".
    """
    dic = _dictionary(dictionary)
    protected_l = {w.lower() for w in protected}
    deny = {k.lower().strip(): v for k, v in (denylist or {}).items()}
    respell_map = {k.lower().strip(): v for k, v in (respell or {}).items()}

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
        # Then the variant respellings, ahead of the known() skip that would
        # otherwise clear them — being in the dictionary is exactly why they
        # need their own channel.
        if wl in respell_map:
            verdicts[wl] = (respell_map[wl], "respell")
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
medium or low whenever the word might be intended.

One kind of item is different: a VARIANT RESPELLING. The word is valid English \
— the dictionary accepts it — but it is another variant's spelling, and this \
manuscript's declared English writes it as the given form (grey -> gray in a \
U.S. book). For those, set is_error true with the given form when the word is \
ordinary prose, at high confidence; keep it (is_error false) only when it is \
part of a proper name (Mr. Grey, the Aldwych Theatre), inside text reproduced \
verbatim (a sign, a letter, a quoted title), or deliberate dialect voice.\
"""


def _build_user(batch: list[tuple[int, Candidate, str]]) -> str:
    lines = []
    for n, cand, sentence in batch:
        if cand.kind == "respell":
            hint = (f'  VARIANT RESPELLING — this manuscript\'s English '
                    f'writes it "{cand.suggestion}"')
        elif cand.suggestion:
            hint = f'  possible correction: "{cand.suggestion}"'
        else:
            hint = ""
        lines.append(f'{n}. word: "{cand.word}"\n'
                     f'   sentence: {sentence}\n{hint}'.rstrip())
    return "\n\n".join(lines)


def site_word_candidates(words: Mapping[str, str],
                         paragraphs: Sequence[ParagraphRef], *,
                         kind: str) -> list[Candidate]:
    """Each word → suggestion pair, sited at every occurrence of the word, as
    Candidates of the given kind. Case-insensitive on the match (the word is a
    specific surface, but a sentence-start or SHOUTED copy is the same word);
    the suggestion is passed through as given — for a demoted name it IS the
    dominant surface, casing and all, so no case-mapping is wanted."""
    cands: list[Candidate] = []
    for word, suggestion in words.items():
        if not word or not suggestion:
            continue
        pat = re.compile(r"(?<![A-Za-z’'])" + re.escape(word)
                         + r"(?![A-Za-z’'])", re.IGNORECASE)
        for p in paragraphs:
            for m in pat.finditer(p.text):
                cands.append(Candidate(
                    para_id=p.para_id, word=m.group(0), start=m.start(),
                    end=m.end(), suggestion=suggestion, kind=kind))
    return cands


# --- recurrence propagation ---------------------------------------------------

# The verbatim surface of a validated edit worth repeating across the book: one
# alphabetic word, or a short run of them joined by single spaces, hyphens or
# apostrophes. This scopes propagation to spelling, word-choice and proper-name
# fixes — the class of error that reads the same at every site — and leaves
# punctuation, spacing, digit and grammar edits (context-specific, and wrong to
# repeat blindly) out of it: a comma inserted in one sentence has no business
# being copied to every other sentence that happens to share its words.
_PROPAGATABLE_SURFACE = re.compile(r"[A-Za-z]+(?:[’'\- ][A-Za-z]+)*\Z")

# A minimal diff can trim to a degenerate surface — a single letter ("B" from a
# "B" -> "B." initial fix) or a bare function word ("and" from "try and" ->
# "try to", once the shared "try " prefix is stripped). Propagating such a
# surface sweeps the whole book with noise the pass exists to prevent (a
# Breniman review raised 179 "and" comments off one idiom fix). Two deterministic
# floors keep propagation to surfaces that actually recur as the same error:
_MIN_PROPAGATE_LEN = 3     # a shorter deleted surface never seeds propagation
_MAX_ASK_SITES = 12        # a real word matching more unclaimed sites than this
                           # is a common word, not a recurring typo — the whole
                           # surface is dropped (logged), never a flood of queries


def _propagatable(delete_text: str, insert_text: str) -> bool:
    """Whether a validated edit's minimal diff is a whole-word/short-phrase swap
    that means the same thing wherever the surface appears."""
    return (bool(delete_text)
            and len(delete_text) >= _MIN_PROPAGATE_LEN
            and delete_text == delete_text.strip()
            and bool(_PROPAGATABLE_SURFACE.match(delete_text))
            and delete_text.lower() != insert_text.strip().lower())


def _word_bounded(surface: str) -> "re.Pattern[str]":
    """A case-insensitive, word-bounded match for a surface — the same boundary
    `site_word_candidates` uses, so a fix never lands inside a longer word."""
    return re.compile(r"(?<![A-Za-z’'])" + re.escape(surface)
                      + r"(?![A-Za-z’'])", re.IGNORECASE)


def propagate_recurrences(validated: Sequence[Finding],
                          paragraphs: Sequence[ParagraphRef], *,
                          dictionary: str = "en_US",
                          protected: Sequence[str] = (),
                          max_sites_per_surface: int = 200,
                          id_prefix: str = "rp") -> list[Finding]:
    """Re-emit every validated word/phrase swap at its other occurrences.

    A deterministic post-pass, generalizing `site_word_candidates`: it reads the
    already-arbitrated `validated` set, collects each edit that swaps one verbatim
    surface for another, and searches the whole document (case-folded, word-
    bounded) for the same surface elsewhere. Each fresh site becomes an ordinary
    Finding the caller re-validates — so the validator dedups any span two sources
    both reach — which closes the catch-it-here-miss-it-there gap outright and
    lifts every detector's reach to the whole book for free.

    Safety is deterministic and layered:
      * Only whole-word/short-phrase alphabetic swaps propagate (`_propagatable`),
        so a comma or a spacing fix never sweeps the book.
      * A surface the validated edits themselves disagree about — changed to X in
        one place and Y in another — is ambiguous by evidence and is dropped
        entirely. This is exactly the effect/affect case: a word that is right in
        some sentences and wrong in others carries conflicting fixes, so nothing
        is swept from it.
      * A surface that is a real dictionary word is context-dependent, so its
        recurrences are raised as margin QUERIES, never silent edits; a genuine
        non-word typo (or a proper-name misspelling) propagates as a tracked
        edit, since the same non-word wants the same fix wherever it appears.
      * Sites the run already speaks for (a validated edit, or an anchored query)
        are skipped, so propagation never fights an existing finding for a span.
      * `protected` (the spell scan's lexicon) is honoured: a coined word the
        author owns is never swept, even if one site happened to be ruled an edit.
    """
    if not validated or not paragraphs:
        return []
    protected_l = {w.lower() for w in protected}
    rank = {"low": 0, "medium": 1, "high": 2}

    # Sites the run already speaks for, per paragraph: a validated edit is fixing
    # the site, and an anchored query has already raised it — propagating onto
    # either would double-edit or second-guess a question already on the record.
    claimed: dict[str, list[tuple[int, int]]] = {}
    for f in validated:
        if f.status in ("validated", "query") and f.anchor is not None:
            claimed.setdefault(f.para_id, []).append(
                (f.anchor.start, f.anchor.end))

    # Seeds: each propagatable surface, and the set of distinct fixes the
    # validated edits proposed for it. A surface with more than one distinct fix
    # is context-dependent and dropped below.
    from .validator import _is_imported     # provenance test; no import cycle
    fixes: dict[str, set[str]] = {}
    base: dict[str, str] = {}     # a fix in its natural casing, for _match_case
    for f in validated:
        if (f.status != "validated" or f.anchor is None or f.format
                or f.force_query):
            continue
        # A curated / imported / replayed row is a HUMAN one-off decision for one
        # site (a TOC line matched to an epigraph, a single word-choice), not a
        # surface-general typo the same everywhere — propagating it floods the
        # margin with queries about every ordinary use of a common word (Purpura:
        # one curated "massive"->"drastic" queried all 9 other "massive"s; okay->OK
        # spawned 4). It must never SEED propagation. (Consistency-choice rows are
        # already skipped above: they are force_query'd, so they never seed
        # either.) A machine detector/sweep row still seeds — that is the recurring
        # typo the pass exists to catch.
        if _is_imported(f):
            continue
        d, ins = f.anchor.delete_text, f.anchor.insert_text
        if not _propagatable(d, ins):
            continue
        key = d.lower()
        if key in protected_l:
            continue
        ins = ins.strip()
        fixes.setdefault(key, set()).add(ins.lower())
        base.setdefault(key, ins)

    surfaces = {k: base[k] for k, fs in fixes.items() if len(fs) == 1}
    if not surfaces:
        return []

    dic = _dictionary(dictionary)
    findings: list[Finding] = []
    dropped: dict[str, int] = {}
    n = 0
    for key in sorted(surfaces):
        fix_base = surfaces[key]
        # A real dictionary word can be correct as written at another site, so
        # its recurrences are asked about rather than silently changed; a non-word
        # (a typo, a misspelled name) wants the same fix wherever it appears.
        ask = _known(dic, key)
        pat = _word_bounded(key)
        # Gather the eligible sites first, so a real-word surface that turns out
        # to be a common word (many sites) can be dropped whole rather than
        # emitting a flood of queries. A non-word typo keeps propagating to every
        # site (bounded only by max_sites_per_surface) — it is the same error
        # wherever it appears.
        sites = []
        for p in paragraphs:
            for m in pat.finditer(p.text):
                start, end = m.start(), m.end()
                if any(s < end and start < e
                       for s, e in claimed.get(p.para_id, ())):
                    continue                         # a finding already has it
                surface = m.group(0)
                fix = _match_case(surface, fix_base)
                if fix == surface:
                    continue                         # a no-op at this casing
                sites.append((p, start, end, surface, fix))
        if ask and len(sites) > _MAX_ASK_SITES:
            # A dictionary word matching this widely is context-dependent, not a
            # recurring typo. Dropping it whole (and saying so) beats burying the
            # real findings under dozens of "may be correct as written" queries.
            log.info('Recurrence propagation: "%s" matches %d site(s) as a '
                     'common word; not propagated (over the %d-site query cap).',
                     key, len(sites), _MAX_ASK_SITES)
            continue
        emitted = 0
        for p, start, end, surface, fix in sites:
            if emitted >= max_sites_per_surface:
                dropped[key] = dropped.get(key, 0) + 1
                continue
            window, lo, occurrence = sentence_window(p.text, start, end)
            corrected = (window[:start - lo] + fix + window[end - lo:])
            n += 1
            emitted += 1
            if ask:
                explanation = (
                    f'"{surface}" was changed to "{fix}" elsewhere in the '
                    f'manuscript; here it may be correct as written, so this '
                    f'is flagged for review rather than changed.')
            else:
                explanation = (
                    f'"{surface}" was corrected to "{fix}" elsewhere in the '
                    f'manuscript; the same spelling here takes the same fix.')
            findings.append(Finding(
                finding_id=f"{id_prefix}-{n:04d}",
                chunk_id="recurrence",
                para_id=p.para_id,
                error_type="spelling",
                original_text=window,
                occurrence=occurrence,
                corrected_text=corrected,
                explanation=explanation,
                # A verbatim recurrence of an already-validated fix is a
                # high-confidence edit; a real-word site is asked instead.
                confidence="medium" if ask else "high",
                force_query=ask))
    for key, lost in sorted(dropped.items()):
        # Never a silent cap: a truncated sweep that says nothing reads as
        # "covered everything", the exact failure the pass exists to prevent.
        log.warning('Recurrence propagation: "%s" hit the %d-site cap; %d '
                    'further occurrence(s) were not propagated.',
                    key, max_sites_per_surface, lost)
    if findings:
        log.info("Recurrence propagation: %d finding(s) from %d surface(s).",
                 len(findings), len(surfaces))
    return findings


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
                usage.add(result.usage, model=model)  # fold serially: not thread-safe
                # Recovery (halve a truncated window, re-ask an unanswered item)
                # runs here, on this thread, so the ordering the folding depends
                # on is untouched. A candidate still unanswered afterwards is
                # counted in `report`, never mistaken for a "not an error".
                rows = resolve_window(
                    window, result, fetch=fetch, rows_of=_rows_of,
                    max_tokens=max_tokens, report=report,
                    usage_sink=lambda ru: usage.add(ru, model=model))
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
        if cand.kind == "respell":
            explanation = (f'"{cand.word}" is a valid spelling, but not this '
                           f'manuscript\'s English variant, which writes '
                           f'"{correction}".')
        elif cand.kind == "near_dup":
            explanation = (f'"{cand.word}" is one letter from '
                           f'"{correction}", the spelling this book uses '
                           f'throughout.')
        else:
            explanation = (f'"{cand.word}" appears to be a misspelling of '
                           f'"{correction}".')
        findings.append(Finding(
            finding_id=f"a-{next(ids):04d}",
            chunk_id="adjudicate",
            para_id=cand.para_id,
            error_type="spelling",
            original_text=para_text,
            occurrence=1,
            corrected_text=corrected_para,
            explanation=explanation,
            confidence=conf,
            # Only a high-confidence call is trusted as a silent edit; a
            # softer one asks rather than changes.
            force_query=rank[conf] < edit_floor))
    return findings
