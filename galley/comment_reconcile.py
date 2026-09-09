"""Reconcile generated proofreading queries against the delivered text.

Only finding-owned comments are candidates for removal. Rebuilding from the
source document preserves the author's existing comments.
"""
from __future__ import annotations

import ast
import re
from typing import Mapping


def proposed_text(row: Mapping) -> str:
    before, after = row.get('original_text', ''), row.get('corrected_text', '')
    if after and after != before:
        return str(after)
    # Historical settlement rows stored their suggestion as a Python string
    # literal in the explanation. Read it as data, never executable content.
    match = re.search(r'— suggested:\s*(.+)$', str(row.get('explanation', '')))
    if match:
        try:
            value = ast.literal_eval(match[1])
            return value if isinstance(value, str) else ''
        except (SyntaxError, ValueError):
            pass
    return ''


def corrected_at_source(source: str, actual: str, quote: str, fix: str,
                        occurrence: int = 1) -> bool:
    """Prove that the proposed text occupies this original target's position."""
    from difflib import SequenceMatcher
    if not quote or not fix or quote == fix:
        return False
    starts = [m.start() for m in re.finditer(re.escape(quote), source)]
    if len(starts) < occurrence or occurrence < 1:
        return False
    lo, hi = starts[occurrence - 1], starts[occurrence - 1] + len(quote)
    projected = []
    for tag, i, j, a, b in SequenceMatcher(a=source, b=actual, autojunk=False).get_opcodes():
        if tag == 'equal':
            if max(i, lo) < min(j, hi):
                projected.append(actual[a + max(i, lo) - i:a + min(j, hi) - i])
        elif tag == 'insert' and lo <= i <= hi:
            projected.append(actual[a:b])
        elif i < hi and j > lo:
            if i < lo or j > hi:
                return False
            projected.append(actual[a:b])
    return ''.join(projected) == fix


def reconciliation(rows, delivered, residuals=(), source=None):
    """Return {stable row key: reason} for provably stale or duplicate queries.

    Matching a replacement somewhere else is insufficient: the original
    target must be absent and its replacement present in the same paragraph.
    Ambiguous/repeated sites are retained for internal judgment.
    """
    from docproof.editmap import row_key
    from galley.settle import settle_residual_of, terminal_state
    from galley.premises import stale_queries
    seen_res = {r['residual_id']: r for r in residuals}
    removed = {str(r.get("finding_id")) if r.get("finding_id") else row_key(r): why for r, why in stale_queries(rows, delivered)}
    seen = set()
    for row in rows:
        if terminal_state(row)[0] != 'query':
            continue
        key = str(row.get("finding_id")) if row.get("finding_id") else row_key(row)
        pid = str(row.get('para_id', ''))
        actual = delivered.get(pid)
        if actual is None:
            continue
        residual = seen_res.get(settle_residual_of(row), {})
        quote = str(residual.get('quote') or row.get('original_text') or '')
        fix = str(residual.get('suggestion') or proposed_text(row))
        if source and corrected_at_source(source.get(pid, ''), actual, quote, fix,
                                          int(row.get('occurrence') or 1)):
            removed[key] = 'already_corrected'
            continue
        # Distinct questions sharing a sentence must survive. For suggestions,
        # identify the same target/fix; otherwise require the same question.
        issue = (pid, quote, int(row.get('occurrence') or 1), fix or
                 ' '.join(str(row.get('explanation') or '').split()).casefold())
        if issue in seen:
            removed[key] = 'duplicate_question'
        seen.add(issue)
    return removed


def actual_comments(path, findings=()):
    """Actual Word comments with finding metadata where exact text matches."""
    from docproof.utils.xml_helpers import DocxPackage, walk_package, qn
    pkg = DocxPackage(path)
    if not pkg.has('word/comments.xml'):
        return []
    anchors = {}
    for para in walk_package(pkg):
        for el in para.element.iter(qn('w:commentRangeStart')):
            anchors[el.get(qn('w:id'))] = para.para_id
    rows = []
    for comment in pkg.tree('word/comments.xml').findall(qn('w:comment')):
        cid = comment.get(qn('w:id'))
        text = ''.join(t.text or '' for t in comment.iter(qn('w:t')))
        pid = anchors.get(cid, '')
        matches = [r for r in findings if r.get('para_id') == pid and
                   str(r.get('explanation') or '').strip() == text.strip()]
        row = dict(matches[0]) if matches else {'error_type': 'preserved_comment'}
        row.update(comment_id=cid, para_id=pid, explanation=text,
                   status='query', queried=True, author=comment.get(qn('w:author')))
        rows.append(row)
    return rows
