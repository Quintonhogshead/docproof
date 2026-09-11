"""Observe an exhausted review budget without making editorial decisions."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    value = json.loads(Path(path).read_text('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Closeout evidence must be a JSON object')
    return value


def close_review_budget(run_dir, *, ledger_path, expected_ledger_sha256,
                        initial_coverage_path, expected_initial_coverage_sha256,
                        source_sha256, config_sha256, round_no,
                        validate_current_coverage):
    """Write a bound, observational settlement after the engine's strict gates.

    The engine must first validate both initial independent reading receipts
    and both complete current reader proofs. This helper independently checks
    actual budget exhaustion, immutable intake, and absence of outstanding
    review calls. It does not apply judgments, claim a completed repair round,
    revise coverage, or turn unfinished work into questions for the author.
    """
    from docproof.resource_ledger import _group_usage, _locked, _read
    from galley.settle import Residual, SETTLEMENT_SCHEMA_VERSION
    from galley.settlement_inputs import verification_dirs
    from galley.verify import (_CHECKPOINT_DIR, _digest, _save_json,
                               build_fingerprints, deliverable_docx,
                               reading_input_snapshot)

    run = Path(run_dir).resolve()
    ledger = Path(ledger_path).resolve()
    initial_path = Path(initial_coverage_path).resolve()
    if type(round_no) is not int or round_no < 0:
        raise ValueError('Closeout requires the actual completed round count')
    with _locked(ledger):
        if _sha(ledger) != expected_ledger_sha256:
            raise ValueError('Review resource ledger changed before closeout')
        events = _read(ledger)
        if not events or any(row.get('source_sha256') != source_sha256 for row in events):
            raise ValueError('Review resource ledger belongs to another source')
        budget = _group_usage(events, 'review')
        exhausted = any(type(budget[k]) is int and budget[k] > 0 and budget[spent] >= budget[k]
                        for k, spent in (('max_calls', 'calls'),
                                         ('max_output_tokens', 'charged_output_tokens')))
        if not exhausted or budget['unknown_output_attempts'] or budget['reserved_output_tokens']:
            raise ValueError('Review budget is not exhausted with fully accounted calls')
        # Historical terminal calls may have unknown usage. Preserve that
        # uncertainty; it is not evidence that the call is still running.
        receipts = {}
        for event in events:
            if event.get('reused'):
                continue
            receipts.setdefault(event['receipt_id'], set()).add(event['status'])
        active = {'started', 'pending', 'recovering'}
        if any(statuses and statuses <= active for statuses in receipts.values()):
            raise ValueError('Outstanding model calls prevent closeout')
        if _sha(initial_path) != expected_initial_coverage_sha256:
            raise ValueError('Original reading intake changed before closeout')
        initial = _json(initial_path)
        if (initial.get('source') != source_sha256 or initial.get('config') != config_sha256
                or initial.get('run') != str(run)
                or initial.get('pass_ids') != ['primary', 'type-compare']
                or not isinstance(initial.get('artifacts'), dict)
                or len(initial['artifacts']) != 4):
            raise ValueError('Original two-pass intake belongs to different inputs')

        protected = {initial_path: expected_initial_coverage_sha256}
        archives = []
        archive_root = (initial_path.parent / 'coverage').resolve()
        for relative, digest in initial['artifacts'].items():
            path = (initial_path.parent / relative).resolve()
            if not path.is_relative_to(archive_root) or _sha(path) != digest:
                raise ValueError('Original full-read artifact is missing or changed')
            protected[path] = digest
            archives.append(path)
        if {(p.parent.name, p.name) for p in archives} != {
                (reader, name) for reader in ('primary', 'type-compare')
                for name in ('change_verify.json', 'finished_walk.json')}:
            raise ValueError('Original two-pass intake is incomplete')

        document = deliverable_docx(run)
        if document is None:
            raise ValueError('Closeout requires the verified manuscript')
        for path in [document, run / 'findings.json', run / 'editmap.json']:
            protected[path] = _sha(path)
        for name in ('reading-input-transitions.json', 'verification-sources.json'):
            if (run / name).exists():
                protected[run / name] = _sha(run / name)
        current_dirs = [run, run / 'verification' / 'type-compare']
        directories = list(dict.fromkeys([*current_dirs, *verification_dirs(run)]))
        artifacts = list(dict.fromkeys([*archives,
            *(directory / name for directory in directories
              for name in ('change_verify.json', 'finished_walk.json'))]))
        issues = {}

        def remember(row, kind, origin):
            if (not isinstance(row, dict) or kind not in {'residual', 'edit_damage'}
                    or not isinstance(row.get('para_id'), str) or not row['para_id']):
                raise ValueError('Malformed unfinished editorial evidence')
            exact = copy.deepcopy(row)
            key = _digest({'kind': kind, 'evidence': exact})
            if key not in issues:
                # Keep the exact original separately from compatibility fields
                # required by existing settlement readers. Same-ID variants
                # with different evidence never inherit another disposition.
                item = (Residual.from_problem(exact) if kind == 'edit_damage'
                        else Residual.from_walk(exact))
                issues[key] = {'kind': kind, 'evidence': exact,
                               'stable_id': item.id, 'origins': []}
            issues[key]['origins'].append(origin)

        for path in artifacts:
            protected[path] = _sha(path)
            payload = _json(path)
            key, kind = (('problems', 'edit_damage') if path.name == 'change_verify.json'
                         else ('residuals', 'residual'))
            if not isinstance(payload.get(key), list):
                raise ValueError('Required verification findings are missing')
            if path.parent in current_dirs and (payload.get('ran') is not True
                    or payload.get('reason') or any(payload.get(k) for k in
                        ('unread_batches', 'unread_paragraphs', 'unverified_paragraphs'))):
                raise ValueError('Current verification is incomplete or dirty')
            proof = payload.get('verification_provenance') or {}
            if path.parent in current_dirs and proof.get('complete') is not True:
                raise ValueError('Current verification has no complete reader proof')
            if proof:
                base = run / _CHECKPOINT_DIR / str(proof.get('scope', '')) / str(proof.get('invocation_id', ''))
                if not base.resolve().is_relative_to((run / _CHECKPOINT_DIR).resolve()):
                    raise ValueError('Invalid reader checkpoint path')
                for request in (proof.get('windows') or {}):
                    if len(request) != 64 or any(c not in '0123456789abcdef' for c in request):
                        raise ValueError('Invalid reader request identity')
                    path_reply = base / (request + '.json')
                    protected[path_reply] = _sha(path_reply)
            for index, row in enumerate(payload[key]):
                remember(row, kind, {'path': str(path), 'collection': key, 'index': index})
        findings = _json(run / 'findings.json')
        if not isinstance(findings.get('findings'), list):
            raise ValueError('Findings are missing from the verified build')
        for index, row in enumerate(findings['findings']):
            pending = row.get('settle_pending_intake')
            if pending is not None:
                if not isinstance(pending, dict):
                    raise ValueError('Malformed durable correction intake')
                remember(pending, pending.get('kind', 'residual'),
                         {'path': str(run / 'findings.json'), 'collection': 'settle_pending_intake',
                          'index': index, 'finding_id': row.get('finding_id')})

        binding = {'source_sha256': source_sha256, 'config_sha256': config_sha256,
                   'ledger_path': str(ledger), 'ledger_sha256': expected_ledger_sha256,
                   'initial_coverage_sha256': expected_initial_coverage_sha256,
                   'protected_files': {str(p): h for p, h in sorted(protected.items())},
                   'build': build_fingerprints(run), 'reading_inputs': reading_input_snapshot(run),
                   'completed_rounds': round_no, 'incomplete_round': round_no + 1,
                   'review_budget': budget,
                   'all_groups': {group: _group_usage(events, group)
                                  for group in sorted({r.get('group', 'book') for r in events})},
                   'observed_ledger_config_sha256': sorted({r.get('config_sha256', '') for r in events})}
        target = run / 'settlement.json'
        previous = _json(target) if target.exists() else None
        generated = previous.get('generated_at') if previous else datetime.now(timezone.utc).isoformat()
        rows = [entry['evidence'] for entry in issues.values()]
        closeout = {'schema_version': 1, 'kind': 'resource_budget_exhausted',
                    'binding': binding, 'binding_sha256': _digest(binding),
                    'issues': [{'evidence_sha256': key, **value} for key, value in issues.items()]}
        payload = {'schema_version': SETTLEMENT_SCHEMA_VERSION, 'generated_at': generated,
                   'run_dir': str(run), 'rounds': round_no, 'max_rounds': round_no + 1,
                   'engine': 'code', 'model': '', 'records': [], 'counts': {},
                   'open': rows, 'residuals_seen': copy.deepcopy(rows),
                   'cost': {'new_api_calls': 0, 'review_api_calls': budget['calls'],
                            'review_charged_output_tokens': budget['charged_output_tokens']},
                   'notes': ['Review resources are exhausted. These original findings remain unfinished '
                             'internal editorial work for final Astra judgment. No editorial decisions '
                             'or author questions were generated by this closeout.'],
                   'convergence': {'stopped': 'resource_budget_exhausted', 'quiet': False,
                                   'rounds': round_no, 'last_new_items': len(rows),
                                   'resource_budget_closeout': closeout}}
        payload['convergence']['closeout_sha256'] = _digest(payload)
        if previous is not None and previous != payload:
            raise ValueError('Existing settlement differs from this bound budget closeout')
        if not callable(validate_current_coverage) or validate_current_coverage() is not True:
            raise ValueError('Current full reading coverage is not validated')
        if _sha(ledger) != expected_ledger_sha256 or any(_sha(p) != h for p, h in protected.items()):
            raise ValueError('Verified inputs changed during budget closeout')
        if previous is None:
            _save_json(target, payload)
        return target
