"""Reconcile generated proofreading queries against the delivered text.

Only finding-owned comments are candidates for removal. Native removal keeps
every existing tracked change, text run, and source comment intact.
"""
from __future__ import annotations

import ast
import re
from typing import Mapping


def _comment_ranges(paragraph):
    """Classic comment ranges in the reject view, including revised spans."""
    from docproof.utils.xml_helpers import qn, VIRTUAL_CHARS, TEXT_SKIP_ANCESTORS
    offset, starts, ranges = 0, {}, {}

    def walk(el):
        nonlocal offset
        for child in el:
            tag = child.tag
            cid = child.get(qn('w:id'))
            if tag == qn('w:commentRangeStart'):
                if cid in starts:
                    raise ValueError('Duplicate comment start')
                starts[cid] = offset
            elif tag == qn('w:commentRangeEnd'):
                if cid not in starts or cid in ranges:
                    raise ValueError('Unpaired comment range')
                ranges[cid] = (starts[cid], offset)
            elif tag == qn('w:ins') or tag in TEXT_SKIP_ANCESTORS:
                continue
            elif tag in (qn('w:t'), qn('w:delText')):
                offset += len(child.text or '')
            elif tag in VIRTUAL_CHARS and (tag != qn('w:tab') or el.tag == qn('w:r')):
                offset += len(VIRTUAL_CHARS[tag])
            else:
                walk(child)
    walk(paragraph)
    return ranges


def _remove_comment_ids(pkg, ids):
    from docproof.utils.xml_helpers import qn
    for part in pkg.names():
        if not part.startswith('word/') or not part.endswith('.xml'):
            continue
        for el in list(pkg.tree(part).iter()):
            if (el.tag in {qn('w:comment'), qn('w:commentRangeStart'),
                           qn('w:commentRangeEnd'), qn('w:commentReference')}
                    and el.get(qn('w:id')) in ids):
                el.getparent().remove(el)
                pkg.mark_modified(part)


def prove_comment_only(before, after, ids):
    """Prove the package differs solely by these comment bodies and markers."""
    from lxml import etree
    from docproof.utils.xml_helpers import DocxPackage
    a, b = DocxPackage(before), DocxPackage(after)
    if a.names() != b.names():
        raise ValueError('Comment removal changed package parts')
    _remove_comment_ids(a, set(ids))
    for part in a.names():
        if part in a._dirty:
            equal = etree.tostring(a.tree(part), method='c14n') == etree.tostring(b.tree(part), method='c14n')
        else:
            equal = a.raw(part) == b.raw(part)
        if not equal:
            raise ValueError('Comment removal changed other manuscript content')


def _owned_comment_ids(document, source, rows, removed):
    from collections import defaultdict
    from types import SimpleNamespace
    from docproof.attribution import PROOFREADER_AUTHOR
    from docproof.editmap import row_key
    from docproof.queries import query_span, query_text
    from docproof.reassembler import paragraph_view_text
    from docproof.utils.xml_helpers import DocxPackage, qn, walk_package
    pkg = DocxPackage(document)
    if any(pkg.has(n) for n in ('word/commentsExtended.xml', 'word/commentsIds.xml',
                               'word/commentsExtensible.xml')):
        raise ValueError('Threaded comments require coordinated reconciliation')
    original = DocxPackage(source)
    protected = ({c.get(qn('w:id')) for c in original.tree('word/comments.xml')}
                 if original.has('word/comments.xml') else set())
    paras = {p.para_id: p for p in walk_package(pkg)}
    ranges = {cid: (pid, *span) for pid, p in paras.items()
              for cid, span in _comment_ranges(p.element).items()}
    actual = defaultdict(list)
    if pkg.has('word/comments.xml'):
        for c in pkg.tree('word/comments.xml'):
            cid = c.get(qn('w:id'))
            if cid in protected or c.get(qn('w:author')) != PROOFREADER_AUTHOR:
                continue
            if cid in ranges:
                text = ''.join(t.text or '' for t in c.iter(qn('w:t')))
                actual[(*ranges[cid], text)].append(cid)
    expected = defaultdict(list)
    for row in rows:
        key = str(row.get('finding_id') or row_key(row))
        if row.get('queried') is False or row.get('unplaced'):
            continue
        if not (row.get('queried') or row.get('status') == 'query'):
            continue
        pid = str(row.get('para_id'))
        if pid not in paras or not row.get('anchor'):
            if key in removed:
                raise ValueError('Removed query has no proven source anchor')
            continue
        finding = SimpleNamespace(**row)
        finding.anchor = SimpleNamespace(**row['anchor'])
        span = query_span(finding, paragraph_view_text(paras[pid].element, 'reject'))
        expected[(pid, *span, query_text(finding))].append(key)
    ids, matched = [], set()
    for signature, keys in expected.items():
        selected = [key for key in keys if key in removed]
        if not selected:
            continue
        candidates = actual[signature]
        if len(candidates) != len(keys):
            raise ValueError('Removed query does not uniquely own its Word comment')
        # Truly identical questions on the same exact span are interchangeable.
        ids.extend(candidates[:len(selected)])
        matched.update(selected)
    missing = {str(r.get('finding_id') or row_key(r)) for r in rows
               if str(r.get('finding_id') or row_key(r)) in removed
               and r.get('queried') is not False and not r.get('unplaced')} - matched
    if missing:
        raise ValueError('Removed query has no matching Word comment')
    return ids


def _atomic_copy(source, target):
    import os
    import shutil
    from pathlib import Path
    temporary = Path(str(target) + '.comment-tmp')
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def recover_comment_transactions(run_dir):
    """Rollback an interrupted multi-file install before any settlement reads."""
    import json
    from pathlib import Path
    from galley.verify import _save_json
    run = Path(run_dir)
    for path in sorted((run / 'settle' / 'comment-transitions').glob('*/transaction.json')):
        state = json.loads(path.read_text('utf-8'))
        if state.get('status') != 'prepared':
            continue
        for name, digest in state['files'].items():
            if Path(name).name != name:
                raise ValueError('Invalid comment transaction path')
            if digest is not None:
                from hashlib import sha256
                if sha256((path.parent / 'before' / name).read_bytes()).hexdigest() != digest:
                    raise ValueError('Comment transaction backup has changed')
        for name, digest in state['files'].items():
            if digest is None:
                (run / name).unlink(missing_ok=True)
            else:
                _atomic_copy(path.parent / 'before' / name, run / name)
        _save_json(path, {**state, 'status': 'rolled_back'})


def remove_queries(run_dir, source, removed, *, reason, intake=None):
    """Stage, prove, and install a comment-only transition with crash rollback.

    Original reading artifacts are backed up without alteration. Only a
    proven package binding is projected into current artifacts; the receipt
    never claims that an old reader saw the new package bytes.
    """
    import copy
    import json
    import shutil
    import uuid
    from pathlib import Path
    from docproof.editmap import row_key
    from docproof.utils.xml_helpers import DocxPackage
    from galley.verify import (_digest, _save_json, applied_edits,
                               build_fingerprints, deliverable_docx, paragraph_views,
                               reading_input_snapshot, record_reading_input_transition,
                               verification_artifact_matches_build)
    run = Path(run_dir)
    recover_comment_transactions(run)
    before_inputs = reading_input_snapshot(run)
    document = deliverable_docx(run)
    env = json.loads((run / 'findings.json').read_text('utf-8'))
    ids = _owned_comment_ids(document, source, env['findings'], removed)
    transition = run / 'settle' / 'comment-transitions' / uuid.uuid4().hex
    before, after = transition / 'before', transition / 'after'
    before.mkdir(parents=True)
    after.mkdir()
    from hashlib import sha256
    files = {}
    for name in (document.name, 'findings.json', 'reading-input-transitions.json',
                 'change_verify.json', 'finished_walk.json'):
        exists = (run / name).exists()
        if not exists and name in ('change_verify.json', 'finished_walk.json'):
            continue
        files[name] = sha256((run / name).read_bytes()).hexdigest() if exists else None
        if exists:
            shutil.copyfile(run / name, before / name)
            shutil.copyfile(run / name, after / name)
    updated = copy.deepcopy(env)
    for row in updated['findings']:
        key = str(row.get('finding_id') or row_key(row))
        if key in removed:
            if row.get('applied'):
                raise ValueError('Cannot remove an applied finding as a query')
            row.update(status='rejected_comment_reconciled', queried=False, applied=False,
                       state='dropped', disposition_reason=removed[key])
            if intake and key in intake:
                row['settle_pending_intake'] = intake[key]
    _save_json(after / 'findings.json', updated)
    pkg = DocxPackage(document)
    _remove_comment_ids(pkg, set(ids))
    pkg.save(after / document.name)
    prove_comment_only(before / document.name, after / document.name, ids)
    if paragraph_views(before) != paragraph_views(after) or applied_edits(before) != applied_edits(after):
        raise ValueError('Comment removal changed reading inputs')
    record_reading_input_transition(after, before_inputs, reason=reason)
    if not (after / 'reading-input-transitions.json').exists():
        files.pop('reading-input-transitions.json')
    receipt = {'schema_version': 1, 'kind': 'comment_only', 'reason': reason,
               'before': build_fingerprints(before), 'after': build_fingerprints(after),
               'applied_edits_sha256': _digest(applied_edits(before)),
               'removed_comment_ids': ids, 'removed_findings': removed,
               'before_findings_sha256': _digest(env), 'after_findings_sha256': _digest(updated)}
    _save_json(transition / 'receipt.json', receipt)
    # Current artifacts become explicit coverage projections of the new
    # package; the full reader identity, responses and original artifact bytes
    # remain intact. Settlement can later project its verdict metadata without
    # pretending that another full reader saw this new package.
    for name in ('change_verify.json', 'finished_walk.json'):
        if name not in files:
            continue
        artifact = json.loads((before / name).read_text('utf-8'))
        if not verification_artifact_matches_build(before, artifact, receipt['before']):
            continue  # Unbound/incomplete legacy evidence gains no authority.
        artifact.update(receipt['after'])
        artifact['comment_only_projection'] = {
            'original_artifact_sha256': files[name],
            'receipt': str((transition / 'receipt.json').relative_to(run)),
            'receipt_sha256': _digest(receipt),
            'before_build_sha256': receipt['before']['build_sha256'],
            'after_build_sha256': receipt['after']['build_sha256']}
        _save_json(after / name, artifact)
    state = {'status': 'prepared', 'files': files}
    _save_json(transition / 'transaction.json', state)
    try:
        for name in state['files']:
            _atomic_copy(after / name, run / name)
        _save_json(transition / 'transaction.json', {**state, 'status': 'committed'})
    except Exception:
        recover_comment_transactions(run)
        raise
    return receipt


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
