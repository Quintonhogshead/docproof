"""The frontier chapter sweep: a loose-prompt whole-chapter proofread.

A second detector with a deliberately different shape from the typed passes.
The chunked review reads paragraph windows under error-type prompts — precise,
cheap, but taxonomy-blind and context-narrow. This pass hands a frontier model
whole chapter-sized windows with one loose instruction — find spelling and
grammar errors — and asks for verbatim ``quote -> correction`` pairs.

Piloted on Redding Book 1 chapter 1 (2026-08-23): the loose frontier read
caught a band of judgment-call errors the typed passes glided past (lay/lie,
everyday/every day, faulty parallelism, suspended hyphens, counterfactual
tense) while missing the mechanical floor the sweeps own — complementary, not
a replacement. See docs/candidate-detector-recovery.md for the wider context.

Nothing here writes to the manuscript. Proposals become
:class:`~docproof.rewrite.RewriteCandidate` rows and ride the same skeptical
``rewrite.confirm`` valve as LanguageTool and Sapling: a confirm judge rules on
each candidate in context, only affirmed errors become tracked changes, and the
shared validator/audit still stand behind everything.
"""
from __future__ import annotations

import logging
from typing import Sequence

from .models import ParagraphRef, Usage
from .rewrite import RewriteCandidate

log = logging.getLogger("docproof.chaptersweep")

SWEEP_SYSTEM = (
    "You are a professional proofreader. Find spelling and grammar errors — "
    "including punctuation and wrong-word errors — in the manuscript excerpt. "
    "Do not restyle, rewrite for taste, or comment on voice; mark only what a "
    "proofreader would call objectively wrong. Each paragraph is numbered "
    "like [P12]. For every error, return the paragraph number, a verbatim "
    "quote of the text containing the error (under 12 words, copied exactly, "
    "including punctuation), the corrected text for exactly that quote, and a "
    "note under 10 words saying why."
)

# The findings contract, enforced by the provider's structured-output layer so
# a malformed reply is retried at the SDK boundary instead of half-parsed here.
SWEEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["para", "quote", "correction", "note"],
                "properties": {
                    "para": {"type": "integer"},
                    "quote": {"type": "string"},
                    "correction": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        }
    },
}


def windows(paragraphs: Sequence[ParagraphRef], *, max_chars: int
            ) -> list[list[tuple[int, ParagraphRef]]]:
    """Pack reviewable paragraphs, in order, into chapter-sized windows.

    Numbering is global (1-based over the reviewable rows), so a finding's
    ``para`` maps straight back to its ParagraphRef regardless of window.
    """
    out: list[list[tuple[int, ParagraphRef]]] = []
    current: list[tuple[int, ParagraphRef]] = []
    size = 0
    n = 0
    for para in paragraphs:
        if not getattr(para, "reviewable", True) or not para.text.strip():
            continue
        n += 1
        cost = len(para.text) + 16
        if current and size + cost > max_chars:
            out.append(current)
            current, size = [], 0
        current.append((n, para))
        size += cost
    if current:
        out.append(current)
    return out


def _payload(rows: list[tuple[int, ParagraphRef]]) -> str:
    return "\n\n".join(f"[P{n}] {p.text}" for n, p in rows)


def _locate(text: str, quote: str) -> tuple[int, int] | None:
    """The quote's span in the paragraph, or None when it cannot be trusted.

    Verbatim first; then a case-insensitive fallback for a model that fixed
    casing inside its own quote. Anything fuzzier fails closed — an unanchored
    proposal is exactly the failure mode the corrections engine taught us
    (86% of its misapplications were location, not judgment).
    """
    at = text.find(quote)
    if at < 0:
        at = text.lower().find(quote.lower())
        if at < 0:
            return None
    return at, at + len(quote)


def propose(paragraphs: Sequence[ParagraphRef], provider, *, model: str,
            max_output_tokens: int, usage: Usage, window_chars: int,
            context: str = "", coverage=None, progress=None,
            stats: dict | None = None,
            concurrency: int = 1) -> list[RewriteCandidate]:
    """Sweep the manuscript window by window and return anchored candidates.

    ``context`` carries the same whole-book prompt sections the typed
    detectors get — the vocabulary (author coinages are not misspellings), the
    variant conventions, and the story sheet (tense, POV, character pronouns)
    — so the loose read judges against the book's own rules instead of
    "correcting" intentional voice.

    A window whose call fails is reported to ``coverage`` and skipped — the
    sweep is additive and must never take the review down with it. Findings
    whose quote cannot be located verbatim in their paragraph are dropped for
    cause and counted, never guessed onto the page.

    ``concurrency`` windows are in flight at once (`docproof.fanout.fan_out`,
    the ladder's pattern): the calls fan out, the replies are folded back in
    WINDOW ORDER on this thread, so candidates come out in document order
    and the usage ledger is touched by one thread. 1 is the plain sequential
    loop this used to be — on Georgis (2026-09-04) seven windows took over
    fifty minutes one at a time, with no line logged per window.
    """
    system = SWEEP_SYSTEM
    if context.strip():
        system = f"{SWEEP_SYSTEM}\n\n{context.strip()}"
    packed = windows(paragraphs, max_chars=window_chars)
    by_number: dict[int, ParagraphRef] = {
        n: p for rows in packed for n, p in rows}
    out: list[RewriteCandidate] = []
    dropped = {"window_failed": 0, "unlocated": 0, "noop": 0, "unknown_para": 0,
               "doubled_word": 0}
    from .fanout import fan_out

    def fetch(indexed):
        _index, rows = indexed
        return provider.complete_structured(
            model=model, system=system, user=_payload(rows),
            schema=SWEEP_SCHEMA, schema_name="chapter_sweep_findings",
            max_tokens=max_output_tokens)

    for (index, rows), result in fan_out(list(enumerate(packed)), fetch,
                                         concurrency=concurrency):
        if result.usage is not None:
            usage.add(result.usage, model=model)
        if result.parsed is None:
            dropped["window_failed"] += 1
            log.warning("Chapter sweep window %d/%d failed (%s); skipping it.",
                        index + 1, len(packed),
                        result.error or result.stop_reason)
            if coverage is not None:
                coverage.note(
                    "Chapter sweep",
                    f"window {index + 1} of {len(packed)} failed "
                    f"({result.error or result.stop_reason}) — the paragraphs "
                    f"in it were not swept", "failed")
            continue
        found_before = len(out)
        for row in result.parsed.get("findings", []):
            para = by_number.get(row.get("para"))
            if para is None:
                dropped["unknown_para"] += 1
                continue
            quote = row.get("quote") or ""
            correction = row.get("correction") or ""
            if not quote or correction == quote:
                dropped["noop"] += 1
                continue
            # A proposal that itself carries a doubled word ("and and,",
            # Georgis 2026-09-04) is the artifact; the validator would
            # refuse it later, but a sweep should not spend a confirm call
            # on it either.
            from .validator import introduces_doubled_word
            if introduces_doubled_word(quote, correction):
                dropped["doubled_word"] += 1
                continue
            span = _locate(para.text, quote)
            if span is None:
                dropped["unlocated"] += 1
                continue
            start, end = span
            out.append(RewriteCandidate(
                para_id=para.para_id, start=start, end=end,
                original=para.text[start:end], replacement=correction,
                note=(row.get("note") or "").strip()[:200] or None))
        log.info("Chapter sweep window %d/%d: %d candidate(s)", index + 1,
                 len(packed), len(out) - found_before)
        if progress:
            progress(index + 1, len(packed))
    if stats is not None:
        stats.update(dropped, windows=len(packed), candidates=len(out))
    if any(dropped.values()):
        log.info("Chapter sweep dropped for cause: %s", dropped)
    return out
