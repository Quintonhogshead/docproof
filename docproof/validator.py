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
                      min_confidence: str) -> list[Finding]:
    paras = index_paragraphs(doc)
    threshold = CONFIDENCE_RANK[min_confidence]
    accepted_spans: dict[str, list[tuple[int, int]]] = {}
    seen: set[tuple[str, int, int, str]] = set()
    out: list[Finding] = []

    for f in findings:
        para = paras.get(f.para_id)
        if para is None:
            out.append(_status(f, "rejected_no_anchor"))
            log.debug("%s: unknown paragraph %s", f.finding_id, f.para_id)
            continue

        # 1 — anchor the verbatim quote
        s = find_nth(para.text, f.original_text, f.occurrence)
        if s == -1:
            out.append(_status(f, "rejected_no_anchor"))
            log.debug("%s: quote not found in %s (occurrence %d): %r",
                      f.finding_id, f.para_id, f.occurrence, f.original_text[:80])
            continue

        # 2 — shrink to the minimal edit
        pre, deleted, inserted = shrink(f.original_text, f.corrected_text)
        if not deleted and not inserted:
            out.append(_status(f, "rejected_noop"))
            continue
        start, end = s + pre, s + pre + len(deleted)
        anchor = Anchor(start=start, end=end,
                        delete_text=deleted, insert_text=inserted)

        # 3 — confidence gate (anchored first, so the report stays informative)
        if CONFIDENCE_RANK[f.confidence] < threshold:
            out.append(_status(f, "skipped_low_confidence", anchor))
            continue

        # 4 — overlap with already-accepted edits in this paragraph
        spans = accepted_spans.setdefault(f.para_id, [])
        if any(_overlaps(start, end, a, b) for a, b in spans):
            out.append(_status(f, "rejected_overlap", anchor))
            continue

        # 5 — exact duplicate
        key = (f.para_id, start, end, inserted)
        if key in seen:
            out.append(_status(f, "rejected_duplicate", anchor))
            continue

        seen.add(key)
        spans.append((start, end))
        out.append(_status(f, "validated", anchor))

    n_ok = sum(1 for f in out if f.status == "validated")
    log.info("Validated %d/%d findings (%s)", n_ok, len(out),
             _tally(out))
    return out


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