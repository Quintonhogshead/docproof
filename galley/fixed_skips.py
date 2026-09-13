"""Freeze unavailable model reads as skipped, never as completed coverage."""
from __future__ import annotations
import hashlib
from pathlib import Path


def _files(folder):
    files = [folder / 'request.json', folder / 'receipt.json']
    if (folder / 'coverage.json').exists():
        files.append(folder / 'coverage.json')
    files.extend(sorted(folder.glob('attempts/*/response.json')))
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def _entries(root, sha):
    from galley import fixed_calls as fc
    budget = fc._load(root / 'budget.json')
    return {k: v for k, v in budget['entries'].items() if v.get('request_sha256') == sha}


def validate_skip(folder, *, request=None):
    from galley import fixed_calls as fc
    folder = Path(folder)
    saved = fc._load(folder / 'skipped.json')
    original = fc._load(folder / 'request.json')
    receipt = fc._load(folder / 'receipt.json')
    entries = _entries(folder.parent.parent, folder.name)
    if (saved.get('version') != 1 or saved.get('status') != 'skipped'
            or saved.get('request_sha256') != folder.name or fc._hash(original) != folder.name
            or request is not None and original != request
            or receipt.get('request_sha256') != folder.name
            or saved.get('stage') != original.get('stage') or saved.get('model') != original.get('model')
            or any(receipt.get(k) != original.get(k) for k in ('stage', 'model', 'transport', 'effort'))
            or any(v.get('model') != original.get('model') or v.get('transport') != original.get('transport') for v in entries.values())
            or saved.get('files') != _files(folder) or saved.get('entries') != entries
            or not isinstance(saved.get('reason'), str) or not saved['reason']):
        raise fc.FixedCallContractError('Skipped-read evidence changed or belongs to another request')
    if receipt.get('status') == 'completed' or any(v.get('status') == 'completed' for v in entries.values()):
        raise fc.FixedCallContractError('A completed read cannot be relabeled as skipped')
    attempts = receipt.get('attempt')
    if (type(attempts) is not int or not 0 <= attempts <= receipt.get('max_attempts', -1) <= 3
            or {v.get('attempt') for v in entries.values()} != set(range(1, attempts + 1))
            or any(v.get('status') not in {'failed', 'started', 'unknown'} for v in entries.values())):
        raise fc.FixedCallContractError('Skipped-read attempt history is inconsistent')
    if receipt.get('coverage_sha256') and not (folder / 'coverage.json').is_file():
        raise fc.FixedCallContractError('Skipped-read coverage contract is missing')
    if (folder / 'coverage.json').exists():
        contract = fc._load(folder / 'coverage.json')
        if (contract.get('request_sha256') != folder.name
                or hashlib.sha256((folder / 'coverage.json').read_bytes()).hexdigest() != receipt.get('coverage_sha256')):
            raise fc.FixedCallContractError('Skipped-read coverage contract changed')
        fc._coverage_contract(contract['coverage'], original['schema'])
    return saved


def freeze_skip(calls, request, reason):
    from galley import fixed_calls as fc
    sha = fc._hash(request)
    folder = calls.directory / 'calls' / sha
    with fc._locked(folder / 'request.lock'), fc._locked(calls.directory / 'budget.lock'):
        if (folder / 'skipped.json').exists():
            return validate_skip(folder, request=request)
        if fc._load(folder / 'request.json') != request:
            raise fc.FixedCallContractError('Cannot skip changed request evidence')
        value = {'version': 1, 'status': 'skipped', 'request_sha256': sha,
                 'stage': request['stage'], 'model': request['model'], 'reason': reason,
                 'files': _files(folder), 'entries': _entries(calls.directory, sha)}
        # Do not rewrite the receipt, raw response, or spending. In particular,
        # unknown submissions retain their full reservation and are not retried.
        fc._atomic(folder / 'skipped.json', value)
        return validate_skip(folder, request=request)


def skipped_result(folder, request):
    from docproof.providers import ProviderResult, NormalizedUsage
    audit = validate_skip(folder, request=request)
    return ProviderResult(parsed={'_skipped_read': {k: audit[k] for k in
        ('status', 'request_sha256', 'stage', 'model', 'reason')}},
        stop_reason='skipped', usage=NormalizedUsage(billed=False), actual_model=request['model'])
