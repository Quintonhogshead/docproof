from __future__ import annotations

import dataclasses
import logging

from .models import (Anchor, CONFIDENCE_RANK, DocumentModel, Finding,
                     index_paragraphs)

log = logging.getLogger("docproof.validator")


def find_nth(haystack: str, needle: str, n: int) -> int:
    """Start offset of the n-th (1-based) occurrence, or -1."""
    start = -1
    for _ in range(n):
        start = haystack.find(needle, start + 1)
        if start == -1:
            return -1
    return start


# Curly quotes, dashes and non-breaking spaces, each folded to its ASCII form
# ONE character for one. The manuscript's normalizer curls quotes before ingest,
# so the canonical text holds “ ” ‘ ’; a weaker model routinely re-types the
# sentence with "straight" punctuation, and an exact substring search then fails
# and the finding is discarded as unanchorable even though it is the same
# sentence character for character. Folding both sides recovers it. The fold is
# deliberately length-preserving — ellipsis (…→...) and whitespace runs are NOT
# folded — so an offset found in the folded text still indexes the real text,
# and the tracked change lands on the manuscript's own characters.
_ANCHOR_FOLD = str.maketrans({
    "“": '"', "”": '"',      # “ ”
    "‘": "'", "’": "'",      # ‘ ’
    "–": "-", "—": "-",      # – —
    " ": " ",                     # non-breaking space
})


def _fold_punct(s: str) -> str:
    return s.translate(_ANCHOR_FOLD)


def anchor_offset(haystack: str, needle: str, n: int) -> int:
    """Start offset of the n-th occurrence of `needle` in `haystack`, or -1.

    Exact first; on failure, a punctuation-tolerant retry that folds curly
    quotes, dashes and nbsp on both sides (see `_ANCHOR_FOLD`). Because the fold
    never moves a character, the offset it returns indexes the ORIGINAL
    `haystack`, so callers slice the real text for the edit."""
    s = find_nth(haystack, needle, n)
    if s != -1:
        return s
    return find_nth(_fold_punct(haystack), _fold_punct(needle), n)


def shrink(original: str, corrected: str) -> tuple[int, str, str]:
    """Trim common prefix and suffix; return (prefix_len, deleted, inserted).
    shrink('It was late, we left.', 'It was late. We left.')
      -> (11, ', w', '. W')"""
    pre = 0
    while (pre < len(original) and pre < len(corrected)
           and original[pre] == corrected[pre]):
        pre += 1
    suf = 0
    while (suf < len(original) - pre and suf < len(corrected) - pre
           and original[len(original) - 1 - suf] == corrected[len(corrected) - 1 - suf]):
        suf += 1
    return pre, original[pre:len(original) - suf], corrected[pre:len(corrected) - suf]


def validate_findings(findings: list[Finding], doc: DocumentModel,
                      min_confidence: str,
                      query_types: frozenset[str] = frozenset(),
                      format_types: dict[str, str] | None = None,
                      edit_guard=None) -> list[Finding]:
    """Anchor every finding, and decide which channel it goes down.

    `query_types` are error types that ask rather than correct. They skip the
    edit machinery entirely: there is no minimal diff to compute, no confidence
    gate to pass (a question is the output, not a fallback from one), and no
    span to claim — a query may sit on text a tracked change also touches,
    because a comment and an edit are different things to a reader.

    `format_types` maps an error type to the run formatting it applies. Those
    findings change how text is set rather than what it says, so there is no
    diff to shrink either, and they claim spans in a register of their own —
    italicising a title and fixing a typo inside it are not in conflict.

    `edit_guard` (an EditGuardConfig) is the overreach safety net: a correction
    that re-types a large span or adds a lot of new text is the model rewriting,
    not proofreading, and is rejected before it can become a tracked change. It
    applies only to the shrink-to-a-diff path — a query or a formatting mark is
    not an edit — and never trips a sweep, whose changes are punctuation-sized.
    None disables it, which is the default so a bare call stays unguarded."""
    format_types = format_types or {}
    paras = index_paragraphs(doc)
    threshold = CONFIDENCE_RANK[min_confidence]
    accepted_spans: dict[str, list[tuple[int, int]]] = {}
    formatted_spans: dict[str, list[tuple[int, int]]] = {}
    seen: set[tuple] = set()
    out: list[Finding] = []

    for f in findings:
        para = paras.get(f.para_id)
        if para is None:
            out.append(_status(f, "rejected_no_anchor"))
            log.debug("%s: unknown paragraph %s", f.finding_id, f.para_id)
            continue

        # 1 — anchor the quote, tolerating a model that re-typed the
        # manuscript's curly punctuation as straight (see anchor_offset)
        s = anchor_offset(para.text, f.original_text, f.occurrence)
        if s == -1:
            out.append(_status(f, "rejected_no_anchor"))
            log.debug("%s: quote not found in %s (occurrence %d): %r",
                      f.finding_id, f.para_id, f.occurrence, f.original_text[:80])
            continue

        if f.error_type in format_types:
            # The text does not change at all, so there is no diff to shrink.
            # corrected_text is the span to mark — a title set in roman that
            # should be italic — and it has to be inside the quoted sentence.
            off = f.original_text.find(f.corrected_text)
            if not f.corrected_text or off == -1:
                out.append(_status(f, "rejected_no_anchor"))
                log.debug("%s: %r is not inside the quoted sentence",
                          f.finding_id, f.corrected_text[:60])
                continue
            start = s + off
            end = start + len(f.corrected_text)
            # The span comes from the real paragraph, not the model's quote, so
            # a fold-recovered match still marks the manuscript's own characters.
            marked = para.text[start:end]
            # Formatting claims its own register: marking a title italic and
            # correcting a typo inside it are not in conflict, but marking the
            # same title twice is.
            spans = formatted_spans.setdefault(f.para_id, [])
            if any(_overlaps(start, end, a, b) for a, b in spans):
                out.append(_status(f, "rejected_duplicate"))
                continue
            if CONFIDENCE_RANK[f.confidence] < threshold:
                out.append(_status(f, "skipped_low_confidence", Anchor(
                    start=start, end=end, delete_text=marked,
                    insert_text=marked)))
                continue
            spans.append((start, end))
            out.append(dataclasses.replace(
                f, status="validated", format=format_types[f.error_type],
                anchor=Anchor(start=start, end=end,
                              delete_text=marked, insert_text=marked)))
            continue

        if f.error_type in query_types:
            # The whole quoted sentence is the anchor: a question is about a
            # passage, not about the characters someone would have changed.
            # The error type is part of the key so two *different* questions
            # about one sentence — a term-consistency query and a speaker-change
            # query, say — both survive; only the same question asked twice is a
            # duplicate.
            key = (f.para_id, s, "query", f.error_type)
            if key in seen:
                out.append(_status(f, "rejected_duplicate"))
                continue
            seen.add(key)
            end = s + len(f.original_text)
            out.append(_status(f, "query", Anchor(
                start=s, end=end,
                delete_text=para.text[s:end], insert_text="")))
            continue

        # 2 — shrink to the minimal edit
        pre, deleted, inserted = shrink(f.original_text, f.corrected_text)
        if not deleted and not inserted:
            out.append(_status(f, "rejected_noop"))
            continue
        start, end = s + pre, s + pre + len(deleted)
        # delete_text is the manuscript's own characters at that span, not the
        # model's quote: when the anchor matched only after folding punctuation
        # the two differ (straight vs curly), and the reassembler re-asserts the
        # slice before it edits. The fold is length-preserving, so this is the
        # same characters in the manuscript's form. (For an exact match it is
        # byte-identical to `deleted`, so nothing changes on that path.)
        anchor = Anchor(start=start, end=end,
                        delete_text=para.text[start:end], insert_text=inserted)

        # 2.5 — overreach guard. A proofreading fix is minimal; an edit that
        # re-types a large span or adds a lot of new text is the model
        # fabricating content or rewriting a passage, and is rejected before it
        # can reach the manuscript. Checked ahead of the confidence gate so an
        # over-large edit is never merely "skipped, low confidence" — it is
        # refused outright, and counted.
        if edit_guard is not None and edit_guard.enabled and _oversteps(
                deleted, inserted, edit_guard):
            out.append(_status(f, "rejected_oversized", anchor))
            log.warning("%s (%s): rejected as an over-large edit — deletes %d, "
                        "inserts %d chars, not a minimal proofreading fix",
                        f.finding_id, f.error_type, len(deleted), len(inserted))
            continue

        # 3 — confidence gate (anchored first, so the report stays informative)
        if CONFIDENCE_RANK[f.confidence] < threshold:
            out.append(_status(f, "skipped_low_confidence", anchor))
            continue

        # 4 — exact duplicate. Asked before overlap on purpose: an identical
        # edit necessarily overlaps the accepted copy of itself, so the other
        # order reports every duplicate as an overlap and this status never
        # fires.
        key = (f.para_id, start, end, inserted)
        if key in seen:
            out.append(_status(f, "rejected_duplicate", anchor))
            continue

        # 5 — overlap with already-accepted edits in this paragraph
        spans = accepted_spans.setdefault(f.para_id, [])
        if any(_overlaps(start, end, a, b) for a, b in spans):
            out.append(_status(f, "rejected_overlap", anchor))
            continue

        seen.add(key)
        spans.append((start, end))
        out.append(_status(f, "validated", anchor))

    n_ok = sum(1 for f in out if f.status == "validated")
    log.info("Validated %d/%d findings (%s)", n_ok, len(out),
             _tally(out))
    return out


def _oversteps(deleted: str, inserted: str, guard) -> bool:
    """Whether a minimal diff is too big to be a proofreading fix: it re-types a
    span wider than the size cap, or it adds more net new text than the growth
    cap. Pure deletions never trip the growth cap — removing text is not
    fabrication."""
    if len(deleted) > guard.max_edit_chars or len(inserted) > guard.max_edit_chars:
        return True
    return len(inserted) - len(deleted) > guard.max_added_chars


def _overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
    """True if spans intersect. Pure insertions (s == e) conflict only when
    strictly inside another span — or with another insertion at the same
    offset; edits that merely touch may coexist."""
    if s1 == e1 and s2 == e2:
        return s1 == s2
    if s1 == e1:
        return s2 < s1 < e2
    if s2 == e2:
        return s1 < s2 < e1
    return s1 < e2 and s2 < e1


def _status(f: Finding, status: str, anchor: Anchor | None = None) -> Finding:
    return dataclasses.replace(f, status=status, anchor=anchor)


def _tally(findings: list[Finding]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))