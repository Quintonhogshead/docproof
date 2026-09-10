"""Evidence-backed correction ledger. No model calls and no remote writes."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from xml.dom import minidom
from zipfile import ZipFile

from .verify import check_saved, prepare_edits, validate_plan


def _load(work, name, issues):
    path = Path(work) / name
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Expected an object')
        return value
    except (OSError, ValueError):
        issues.append(f'{name} could not be read; its contents are unconfirmed.')
        return {}


def _rows(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _join(value):
    return '\n'.join(map(str, value)) if isinstance(value, (list, tuple)) else str(value or '')


def _evidence_text(row):
    parts = []
    if row.get('text'):
        parts.append(str(row['text']))
    for key in ('annotation', 'comment', 'table'):
        if row.get(key):
            parts.append(json.dumps(row[key], ensure_ascii=False, indent=2))
    meaningful_runs = [r for r in row.get('runs', []) if r.get('revision') or r.get('formatting')]
    if meaningful_runs:
        parts.append('Formatting / revisions: ' + json.dumps(meaningful_runs, ensure_ascii=False))
    if not parts:
        parts.append(str(row.get('note') or 'Visual or unread evidence; consult the submitted file.'))
    return '\n'.join(parts)


def _location(row):
    if type(row.get('page_index')) is int:
        return f"PDF/image page {row['page_index'] + 1}"
    if row.get('paragraph_id'):
        return 'Paragraph ' + str(row['paragraph_id']).rsplit('-', 1)[-1]
    return str(row.get('kind', '')).replace('_', ' ')


def build_audit(work: Path, result: dict, plan=None, *, context=None) -> dict:
    """Project all saved evidence, including omissions and pre-plan failures."""
    work = Path(work)
    issues = []
    packet = _load(work, 'packet.json', issues)
    submission = _load(work, 'submission.json', issues)
    receipt = _load(work, 'workflow.json', issues)
    baseline = _load(work, 'baseline.json', issues)
    final = _load(work, 'final.json', issues) or _load(work, 'saved.json', issues)
    review = _load(work, 'review.json', issues)
    saved_plan = _load(work, 'plan.json', issues)
    plan = plan if isinstance(plan, dict) else saved_plan
    job = context if isinstance(context, dict) else _load(work.parent, 'job.json', issues)
    source_name = Path(submission.get('source') or job.get('source_name') or result.get('source_name') or '').name
    frozen_artifacts_intact = True
    if receipt.get('stage') == 'complete':
        for filename, sha in (receipt.get('artifact_hashes') or {}).items():
            path = Path(filename)
            try:
                if not path.resolve().is_relative_to(work.resolve()):
                    raise ValueError('Artifact outside job')
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != sha:
                        raise ValueError('Changed artifact')
            except (OSError, ValueError):
                frozen_artifacts_intact = False
                issues.append(f'Completed artifact changed or is missing: {path.name}. Saved-change proof is no longer current.')
    instructions, edits = _rows(plan.get('instructions')), _rows(plan.get('edits'))
    evidence, sources = _rows(packet.get('evidence')), _rows(packet.get('sources'))
    valid = False
    try:
        validate_plan(plan, packet)
        prepare_edits(baseline, edits)
        valid = True
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        issues.append('No validated correction plan.' if not instructions else f'Plan validation: {exc}')
    coverage = defaultdict(list)
    for instruction in instructions:
        for eid in instruction.get('covered_evidence_ids', []):
            coverage[str(eid)].append(str(instruction.get('id', '')))
    from .astra import _required_evidence_ids
    required = _required_evidence_ids(packet)
    by_evidence = {str(r.get('id')): r for r in evidence}
    source_names = {str(s.get('id')): str(s.get('name', '')) for s in sources}
    stage = receipt.get('stage', 'before apply')
    applied = (work / 'applied.json').is_file()
    verification, expected, resolved = {}, {}, []
    try:
        if valid and final and applied and stage != 'applying' and frozen_artifacts_intact:
            expected, resolved = prepare_edits(baseline, edits)
            verification = check_saved(baseline, final, edits)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        issues.append(f'Saved-document verification could not be reproduced: {exc}')
    after = {str(r.get('id')): r for r in _rows(final.get('stories'))}
    inventory_complete = final.get('style_inventory_complete') is not False and baseline.get('style_inventory_complete') is not False
    formatting = bool(verification and inventory_complete and not any(
        not str(f).startswith(('Unavailable font:', 'Unresolved artwork:', 'The corrected document contains overflowing text.'))
        for f in verification.get('failures', [])))
    reviewed_ids = review.get('instruction_ids', [])
    review_complete = bool(review and set(reviewed_ids) == {i.get('id') for i in instructions}
                           and len(reviewed_ids) == len(set(reviewed_ids))
                           and 'required_review_pages' in final
                           and set(review.get('reviewed_pages', [])) == set(final['required_review_pages']))
    ready = bool(valid and verification.get('passed') and inventory_complete and review_complete
                 and review.get('status') == 'verified' and result.get('status') == 'verified')
    changes = []
    edit_text_proof = {}
    owned = {eid: i.get('id') for i in instructions for eid in i.get('edit_ids', [])}
    resolved_by_id = defaultdict(list)
    for r in resolved:
        resolved_by_id[r['id']].append(r)
    for edit in edits:
        sid, eid = str(edit.get('story_id', '')), str(edit.get('id', ''))
        proven = bool(applied and stage != 'applying' and sid in expected and sid in after
                      and after[sid].get('text') == expected[sid])
        edit_text_proof[eid] = proven
        changes.append({'Instruction': owned.get(eid, 'Unowned edit'), 'Edit': eid, 'Story': sid,
                        'Original text': edit.get('find', ''), 'Requested replacement': edit.get('replacement', ''),
                        'Saved replacement': edit.get('replacement', '') if proven else 'Unconfirmed',
                        'Expected occurrences': edit.get('expected_count', 1),
                        'Confirmed occurrences': len(resolved_by_id[eid]) if proven else None,
                        'Text confirmed': 'Yes' if proven else 'No',
                        'Formatting confirmed': 'Yes' if proven and formatting else 'Unconfirmed',
                        'Requested formatting': json.dumps({k: edit[k] for k in ('font_style', 'style_ranges') if edit.get(k)}, ensure_ascii=False),
                        'Original offsets (0-based)': _join([f"{r['start']}:{r['end']}" for r in resolved_by_id[eid]])})
    correction_rows = []
    for item in instructions:
        iid, disp = str(item.get('id', '')), item.get('disposition')
        ids = item.get('edit_ids', [])
        proven = bool(ids and all(edit_text_proof.get(e) for e in ids))
        if not valid:
            status = 'Unconfirmed — invalid plan'
        elif disp == 'designer':
            status = 'Not done — designer needed'
        elif disp == 'clarification':
            status = 'Not done — clarification needed'
        elif disp == 'already_correct':
            status = 'No edit — reviewed' if review_complete else 'No edit — planner assessment only'
        elif stage == 'applying':
            status = 'Unconfirmed — apply interrupted'
        elif not applied:
            status = 'Not done — not applied'
        elif not proven:
            status = 'Unconfirmed — saved text not verified'
        elif ready:
            status = 'Done — verified'
        else:
            status = 'Applied — book review required'
        refs = [by_evidence[e] for e in item.get('covered_evidence_ids', []) if e in by_evidence]
        correction_rows.append({'Instruction': iid, 'Status': status,
            'Requested correction / source wording': '\n\n'.join(_evidence_text(r) for r in refs) or
                'See cited source files; this older plan has no evidence-level references.',
            'Reason / remaining work': item.get('reason', ''),
            'Saved text confirmed': 'Yes' if proven else 'No edit' if disp != 'edit' else 'No',
            'Formatting confirmed': 'Yes' if proven and formatting else 'No edit' if disp != 'edit' else 'Unconfirmed',
            'Source files': _join([source_names.get(str(s), str(s)) for s in item.get('source_ids', [])]),
            'Source locations': _join([_location(r) for r in refs]), 'Edit IDs': _join(ids),
            'Evidence IDs': _join(item.get('covered_evidence_ids', [])),
            'Final review coverage': 'Covered; whole-book verdict only' if review_complete and iid in reviewed_ids else 'Unconfirmed',
            'Book outcome': str(result.get('status', 'unknown')).replace('_', ' ')})
    for eid in sorted(required):
        if len(coverage[eid]) != 1:
            row = by_evidence[eid]
            correction_rows.append({'Instruction': 'Evidence: ' + eid,
                'Status': 'Not done — evidence unaccounted for',
                'Requested correction / source wording': _evidence_text(row),
                'Reason / remaining work': 'No instruction covers this evidence.' if not coverage[eid] else 'Multiple instructions claim this evidence; reconcile the overlap.',
                'Source files': source_names.get(str(row.get('source_id')), ''), 'Source locations': _location(row), 'Evidence IDs': eid})
    for eid in sorted(set(coverage) - set(by_evidence)):
        issues.append(f'Unknown evidence reference: {eid}')
    if not correction_rows:
        correction_rows.append({'Instruction': 'Intake', 'Status': 'Not done — blocked before planning',
            'Requested correction / source wording': submission.get('text') or job.get('submission_text') or 'Correction content has not been read. See Files for all submitted slots.',
            'Reason / remaining work': _join(result.get('reasons')) or 'No validated item-level plan is available; the number of corrections is unknown.'})
    evidence_rows = []
    for row in evidence:
        eid = str(row.get('id', ''))
        evidence_rows.append({'Evidence': eid, 'Source file': source_names.get(str(row.get('source_id')), ''),
            'Location': _location(row), 'Required': 'Yes' if eid in required else 'Context',
            'Coverage': 'Context — no instruction required' if eid not in required and not coverage[eid] else 'Covered once' if len(coverage[eid]) == 1 else 'Uncovered' if not coverage[eid] else 'Covered more than once',
            'Instructions': _join(coverage[eid]), 'Complete extracted content': _evidence_text(row),
            'Source ID': row.get('source_id', '')})
    files = []
    attachment_receipts = job.get('attachment_receipts') or {}
    urls = job.get('submission_urls') or []
    if urls or attachment_receipts:
        count = max(len(urls), int(job.get('expected_attachment_count') or 0), len(attachment_receipts))
        origin = []
        for event in _rows(job.get('submission_receipts')):
            origin.extend([str(event.get('marker', ''))] * len(event.get('urls', [])))
        for index in range(count):
            r = attachment_receipts.get(str(index), {})
            matches = [s for s in sources if s.get('sha256') and s.get('sha256') == r.get('sha256')]
            files.append({'Slot': index + 1, 'Submission': origin[index] if index < len(origin) else job.get('submission_marker', ''),
                'Filename': r.get('filename', f'Attachment {index+1}'),
                'Receipt': 'Received duplicate' if r.get('duplicate_of') is not None else 'Received' if r else 'Missing / not read',
                'Duplicate of slot': int(r['duplicate_of']) + 1 if r.get('duplicate_of') is not None else None,
                'Bytes': r.get('bytes'), 'SHA-256': r.get('sha256', ''), 'File ID': str(r.get('file_id') or ''),
                'Source IDs': _join([s.get('id', '') for s in matches]),
                'Read errors': _join([e.get('message', str(e)) for s in matches for e in s.get('errors', [])])})
    else:
        attachments = _rows(submission.get('attachments')) or [s for s in sources if s.get('kind') != 'text'] or _rows(result.get('submitted_attachments'))
        seen = {}
        for index, row in enumerate(attachments, 1):
            sha = row.get('sha256')
            matches = [s for s in sources if s.get('path') == row.get('path') or sha and s.get('sha256') == sha]
            files.append({'Slot': index, 'Filename': row.get('name') or Path(row.get('path') or '').name,
                'Receipt': 'Received duplicate' if sha in seen else 'Received' if sha else 'Missing / not read',
                'Duplicate of slot': seen.get(sha), 'SHA-256': sha or '',
                'Source IDs': _join([s.get('id', '') for s in matches]),
                'Read errors': _join([e.get('message', str(e)) for s in matches for e in s.get('errors', [])])})
            if sha:
                seen.setdefault(sha, index)
    for source in sources:
        if source.get('kind') == 'text':
            files.append({'Slot': 'Notes', 'Filename': source.get('name', 'Form notes'), 'Receipt': 'Read',
                          'SHA-256': source.get('sha256', ''), 'Source IDs': source.get('id', '')})
    # Every unplanned source remains visible even when extraction failed before
    # it could yield required evidence units.
    used_sources = {str(s) for i in instructions for s in i.get('source_ids', [])}
    for source in sources:
        if str(source.get('id')) not in used_sources and source.get('kind') != 'text':
            issues.append(f"Source not accounted for by the plan: {source.get('name', source.get('id'))}")
    issues.extend(str(e.get('message') or e) if isinstance(e, dict) else str(e) for e in packet.get('errors', []))
    issues.extend(map(str, verification.get('failures', [])))
    outcome_notes = list(dict.fromkeys(map(str, result.get('reasons', []) + review.get('reasons', []))))
    details = [{'Check': 'Outcome', 'Result': str(result.get('status', 'unknown')).replace('_', ' ')},
        {'Check': 'Run ID', 'Result': job.get('job_id') or work.name},
        {'Check': 'Report created (UTC)', 'Result': 'UTC: ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')},
        {'Check': 'Source book', 'Result': source_name or 'Not established'},
        {'Check': 'Output book', 'Result': Path(result.get('output_indd') or '').name or 'Not established'},
        {'Check': 'Source SHA-256', 'Result': submission.get('source_sha256') or job.get('source_sha256', '')},
        {'Check': 'Registered author / title', 'Result': _join([(job.get('book_identity') or {}).get('author', ''), (job.get('book_identity') or {}).get('title', '')])},
        {'Check': 'Project / source / folder IDs', 'Result': _join([job.get('record_id', ''), job.get('source_id', ''), job.get('folder_id', '')])},
        {'Check': 'Submission IDs', 'Result': _join([r.get('marker', '') for r in _rows(job.get('submission_receipts'))])},
        {'Check': 'Expected / received attachment slots', 'Result': f"{job.get('expected_attachment_count', len([r for r in files if r.get('Slot') != 'Notes']))} / {job.get('received_attachment_count', len([r for r in files if r.get('Receipt', '').startswith('Received')]))}"},
        {'Check': 'Plan valid', 'Result': 'Yes' if valid else 'No'},
        {'Check': 'Workflow stage', 'Result': stage},
        {'Check': 'Apply returned', 'Result': 'Yes' if applied else 'No'},
        {'Check': 'Saved text and document checks', 'Result': 'Passed' if verification.get('passed') else 'Not passed / unconfirmed'},
        {'Check': 'Formatting checks', 'Result': 'Passed' if formatting else 'Not passed / unconfirmed'},
        {'Check': 'Final review', 'Result': review.get('status', 'Not completed')},
        {'Check': 'All instructions and required pages reviewed', 'Result': 'Yes' if review_complete else 'No'},
        {'Check': 'Required / reviewed PDF pages', 'Result': f"{_join(final.get('required_review_pages', []))}\nReviewed:\n{_join(review.get('reviewed_pages', []))}"},
        {'Check': 'Delivery', 'Result': 'Correction audit only. Upload status is recorded separately in the delivery receipt.'},
        {'Check': 'Status definitions', 'Result': 'Done: saved text and formatting checks plus whole-book review passed. Applied: exact saved story text matches; book is still held. No edit: planner assessment, not a change. Unconfirmed: do not count as completed. Evidence and Files include contextual material as well as requests.'}]
    details.extend({'Check': f'Issue {n}', 'Result': reason} for n, reason in enumerate(dict.fromkeys(issues), 1))
    details.extend({'Check': f'Outcome / review note {n}', 'Result': reason}
                   for n, reason in enumerate([r for r in outcome_notes if r not in issues], 1))
    for field in ('output_indd', 'output_pdf', 'output_idml'):
        path = Path(result[field]) if result.get(field) else None
        if path and path.is_file():
            with path.open('rb') as stream:
                details.append({'Check': path.name + ' SHA-256', 'Result': hashlib.file_digest(stream, 'sha256').hexdigest()})
    return {'schema_version': 1, 'outcome': result.get('status', 'unknown'),
        'source_book': source_name,
        'output_book': Path(result.get('output_indd') or '').name,
        'instruction_count': len(instructions) if valid else None,
        'corrections': correction_rows, 'changes': changes, 'evidence': evidence_rows, 'files': files, 'details': details}


def _runtime():
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node = os.environ.get('DOCPROOF_AUDIT_NODE') or (str(bundled / 'bin/node.exe') if (bundled / 'bin/node.exe').is_file() else shutil.which('node'))
    modules = os.environ.get('DOCPROOF_AUDIT_MODULES') or (str(bundled / 'node_modules') if (bundled / 'node_modules/@oai/artifact-tool').is_dir() else '')
    if not node:
        raise RuntimeError('The corrections spreadsheet needs Node.js and @oai/artifact-tool on this worker.')
    return node, modules


def _preserve_literal_cells(path):
    """Repair a missing exporter capability: date-like strings become numbers.

    Layout and calculations are authored by artifact-tool. Only its quoted
    constant-string cells are serialized as native XLSX inline text here; all
    calculated formulas and their caches remain untouched. This also makes
    author text beginning with '=' inert, ordinary spreadsheet text.
    """
    fixed = path.with_name('literal-audit.xlsx')
    with ZipFile(path) as source, ZipFile(fixed, 'w') as destination:
        for entry in source.infolist():
            raw = source.read(entry.filename)
            if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', entry.filename):
                document = minidom.parseString(raw)
                namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
                for cell in document.getElementsByTagNameNS(namespace, 'c'):
                    formulas = cell.getElementsByTagNameNS(namespace, 'f')
                    if not formulas:
                        continue
                    formula = ''.join(n.data for n in formulas[0].childNodes if n.nodeType == n.TEXT_NODE)
                    if not re.fullmatch(r'"(?:[^"]|"")*"', formula):
                        continue
                    text = formula[1:-1].replace('""', '"')
                    for child in list(cell.childNodes):
                        if child.nodeType == child.ELEMENT_NODE and child.localName in {'f', 'v', 'is'}:
                            cell.removeChild(child)
                    cell.setAttribute('t', 'inlineStr')
                    prefix = cell.prefix + ':' if cell.prefix else ''
                    inline = document.createElementNS(namespace, prefix + 'is')
                    value = document.createElementNS(namespace, prefix + 't')
                    value.setAttribute('xml:space', 'preserve')
                    value.appendChild(document.createTextNode(text))
                    inline.appendChild(value)
                    cell.appendChild(inline)
                raw = document.toxml(encoding='utf-8')
                document.unlink()
            destination.writestr(entry, raw)
    os.replace(fixed, path)


def write_audit(work: Path, result: dict, plan=None, *, context=None, destination=None, render_dir=None) -> Path:
    """Atomically publish the XLSX; failure blocks completion and delivery."""
    work = Path(work)
    destination = Path(destination) if destination else work / 'correction-audit.xlsx'
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = build_audit(work, result, plan, context=context)
    node, modules = _runtime()
    with tempfile.TemporaryDirectory(prefix='audit-', dir=destination.parent) as temporary:
        root = Path(temporary)
        payload, output = root / 'audit.json', root / 'audit.xlsx'
        payload.write_text(json.dumps(data, ensure_ascii=True, allow_nan=False), encoding='utf-8')
        command = [node, str(Path(__file__).with_name('audit_workbook.mjs')), str(payload), str(output), modules]
        if render_dir:
            command.append(str(Path(render_dir).resolve()))
        response = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if response.returncode or not output.is_file() or not output.stat().st_size:
            raise RuntimeError('The corrections spreadsheet could not be generated; delivery is blocked. Check the local spreadsheet runtime.')
        _preserve_literal_cells(output)
        os.replace(output, destination)
    return destination
