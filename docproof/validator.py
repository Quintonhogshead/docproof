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
# and the tracked change lands on the manuscript's own characters. The
# continuity read's two-quote guardrail locates with this same fold
# (continuity._locate), so the two passes never disagree about what counts as
# the same sentence.
_ANCHOR_FOLD = str.maketrans({
    "“": '"', "”": '"',      # “ ”
    "‘": "'", "’": "'",      # ‘ ’
    "–": "-", "—": "-",      # – —
    " ": " ",                     # non-breaking space
})


def fold_punct(s: str) -> str:
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
    return find_nth(fold_punct(haystack), fold_punct(needle), n)


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

        if f.error_type in query_types or f.force_query:
            # The whole quoted sentence is the anchor: a question is about a
            # passage, not about the characters someone would have changed.
            # force_query is a downgrade — the overseer-verifier's, or the
            # meaning gate's — a finding neither would keep as a change but
            # neither rejected, sent to the margin instead.
            # The error type is part of the key so two *different* questions
            # about one sentence — a term-consistency query and a speaker-change
            # query, say — both survive; only the same question asked twice is a
            # duplicate. A downgrade carries a specific correction that was
            # withheld, so the correction is part of what makes it a distinct
            # question: two different fixes for one sentence, both held back,
            # are two things to tell the author, and keying them the same way
            # would silently drop the second — the one outcome a downgrade must
            # never produce.
            key = (f.para_id, s, "query", f.error_type,
                   f.corrected_text if f.force_query else "")
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


def to_query(f: Finding, doc: DocumentModel) -> Finding:
    """Re-cut an already-validated edit as a margin question, in place.

    The meaning gate needs to withdraw a change AFTER the run has been
    arbitrated, and it must do so without re-running the validator: a second
    arbitration re-opens every span, which can promote an edit that was set
    aside as overlapping and let it evict a change the gate had just approved.
    So the gate withdraws its findings one at a time, through here — the span
    stays claimed, nothing else moves, and the only difference to the run is
    that this correction became a question. Same anchoring the query branch of
    `validate_findings` uses.

    A finding whose quote no longer anchors (it did when it validated, so this
    is belt and braces) falls back to the whole paragraph as its span. It has to
    get one. A query with no anchor is NOT "still reported, not applied" — both
    reassemblers place a margin comment from `Finding.anchor` and drop a query
    that has none, so an anchorless question survives in findings.json and in
    summary.md while being absent from the file the author opens, which is the
    one place summary.md promises it will be. The paragraph is the coarsest
    honest answer to "where", it is always attachable, and it still edits
    nothing — so the question reaches the reader and the safe direction holds.

    Only an unknown paragraph comes back with no anchor, because there is then
    nothing at all to hang a comment on; the reassemblers count that as
    `unplaced` rather than dropping it quietly."""
    para = index_paragraphs(doc).get(f.para_id)
    if para is None:
        log.warning("%s: unknown paragraph %s — withdrawn to the margin with "
                    "no anchor, so no comment can be written for it.",
                    f.finding_id, f.para_id)
        return dataclasses.replace(f, status="query", anchor=None,
                                   force_query=True)
    s = anchor_offset(para.text, f.original_text, f.occurrence)
    if s == -1:
        log.warning("%s: quote no longer anchors in %s — the question is hung "
                    "on the whole paragraph.", f.finding_id, f.para_id)
        s, end = 0, len(para.text)
    else:
        end = s + len(f.original_text)
    return dataclasses.replace(
        f, status="query", force_query=True,
        anchor=Anchor(start=s, end=end, delete_text=para.text[s:end],
                      insert_text=""))


def _oversteps(deleted: str, inserted: str, guard) -> bool:
    """Whether a minimal diff is too big to be a proofreading fix: it re-types a
    span wider than the size cap, or it adds more net new text than the growth
    cap. Pure deletions never trip the growth cap — removing text is not
    fabrication."""
    if len(deleted) > guard.max_edit_chars or len(inserted) > guard.max_edit_chars:
        return True
    return len(inserted) - len(deleted) > guard.max_added_chars


def _overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
    """True if two edits conflict and cannot both apply to one paragraph.

    Two insertions conflict only at the same point. An insertion conflicts with
    a replacement or deletion when its point is the span's START or lies inside
    it — the half-open range [start, end) — because both then act at the same
    offset and compose order-dependently into duplicated or garbled text:
    inserting "to " at the start of a "could"->"to" replacement composes to
    "to to", and a comma inserted at the start of a " they were"->", he was"
    replacement composes to ",,". An insertion that merely abuts a span's END
    sits after it and composes cleanly ("colour"->"color" with a "," inserted at
    the close gives "color,"), so it does not conflict. Two non-empty spans
    conflict when their intervals intersect; spans that only touch end-to-start
    (a [3, 5] beside a [5, 8]) do not."""
    if s1 == e1 and s2 == e2:          # two insertions: only at the same point
        return s1 == s2
    if s1 == e1:                        # s1 an insertion into the s2..e2 span
        return s2 <= s1 < e2
    if s2 == e2:                        # s2 an insertion into the s1..e1 span
        return s1 <= s2 < e1
    return s1 < e2 and s2 < e1         # two spans: half-open interval overlap


def _status(f: Finding, status: str, anchor: Anchor | None = None) -> Finding:
    return dataclasses.replace(f, status=status, anchor=anchor)


def _tally(findings: list[Finding]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))