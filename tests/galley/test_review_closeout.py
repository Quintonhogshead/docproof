"""Budget closeout records unfinished work without rewriting verified input."""
import copy
import hashlib
import json
import shutil

import pytest

from docproof.models import Usage
from docproof.providers import ProviderResult, NormalizedUsage
from galley import verify
from galley.review_closeout import close_review_budget
from tests.galley.test_verification_checkpoints import run, isolated_coverage


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def case(run, tmp_path):
    pid = next(iter(verify.accepted_text(run)))
    original = {'para_id': pid, 'quote': 'dog', 'problem': 'Check the animal.',
                'suggestion': 'hound', 'severity': 'medium'}
    variant = {**original, 'suggestion': 'cat'}

    class Provider:
        name = 'fake'
        effort = 'high'
        def complete_structured(self, **kwargs):
            return ProviderResult(parsed={'findings': [self.row]} if kwargs['schema_name'] == 'findings'
                                  else {'problems': []}, usage=NormalizedUsage(10, 5))

    artifacts = {}
    engine = tmp_path / 'driver' / 'engine'
    engine.mkdir(parents=True)
    for pass_id, row in [('primary', original), ('type-compare', variant)]:
        provider = Provider()
        provider.row = row
        options = dict(engine='provider', pass_id=pass_id,
                       required_pass_ids=('primary', 'type-compare'),
                       policy_id=verify.VERIFICATION_POLICY, config_sha256='c' * 64)
        uc, uw = Usage(), Usage()
        changes = verify.verify_run(run, provider, 'fake-model', uc, run_walk=False, **options)
        walk = verify.verify_run(run, provider, 'fake-model', uw, run_changes=False, **options)
        out = run if pass_id == 'primary' else run / 'verification' / pass_id
        verify.write_artifacts(out, changes, walk, model='fake-model', engine='provider',
                               usage_changes=uc, usage_walk=uw, applied=1, paragraphs=1,
                               source_run_dir=run)
        for name in ('change_verify.json', 'finished_walk.json'):
            target = engine / 'coverage' / '0' / pass_id / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out / name, target)
            artifacts[str(target.relative_to(engine))] = sha(target)
    findings = json.loads((run / 'findings.json').read_text())
    pending = {**original, 'suggestion': 'wolf', 'residual_id': verify.residual_id(pid, 'dog'),
               'kind': 'residual'}
    findings['findings'].append({'finding_id': 'intake', 'para_id': pid, 'state': 'dropped',
                                 'settle_pending_intake': pending})
    findings['findings'].append({'finding_id': 'author-question', 'para_id': pid,
                                 'status': 'query', 'queried': True, 'explanation': 'Who owns the dog?'})
    (run / 'findings.json').write_text(json.dumps(findings))
    (run / 'editmap.json').write_text('{"preserved":"edit mapping"}')
    initial = engine / 'initial-coverage.json'
    initial.write_text(json.dumps({'source': 's' * 64, 'config': 'c' * 64, 'run': str(run),
                                   'pass_ids': ['primary', 'type-compare'], 'artifacts': artifacts}))
    ledger = engine.parent / 'resources.jsonl'
    events = []
    for n in range(2):
        base = dict(source_sha256='s' * 64, config_sha256='canonical-config',
                    receipt_id=str(n), operation_id=str(n), model='fake', group='review',
                    reused=False, included_operation_ids=[], reservation_output_tokens=50,
                    budget={'max_calls': 2, 'max_output_tokens': 1000})
        events.extend([{**base, 'status': 'started', 'usage': None},
                       {**base, 'status': 'completed', 'usage': {'output_tokens': 10}}])
    ledger.write_text(''.join(json.dumps(e) + '\n' for e in events))
    kwargs = dict(ledger_path=ledger, expected_ledger_sha256=sha(ledger),
                  initial_coverage_path=initial, expected_initial_coverage_sha256=sha(initial),
                  source_sha256='s' * 64, config_sha256='c' * 64, round_no=0,
                  validate_current_coverage=lambda: True)
    return run, kwargs, events, pending


def protected(case):
    run, kwargs, *_ = case
    root = run.parent
    return {p: p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}


def test_closeout_preserves_every_evidence_variant_and_all_verified_bytes(case):
    run, kwargs, events, pending = case
    before = protected(case)
    path = close_review_budget(run, **kwargs)
    data = json.loads(path.read_text())
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert data['rounds'] == 0
    assert data['records'] == []
    assert data['convergence']['stopped'] == 'resource_budget_exhausted'
    assert {r['suggestion'] for r in data['open']} == {'hound', 'cat', 'wolf'}
    assert len({i['stable_id'] for i in data['convergence']['resource_budget_closeout']['issues']}) == 1
    assert data['residuals_seen'] == data['open']
    proof = data['convergence']['resource_budget_closeout']
    assert proof['binding']['review_budget']['calls'] == 2
    assert proof['binding']['review_budget']['max_calls'] == 2
    assert proof['binding']['incomplete_round'] == 1
    assert any(issue['evidence'] == pending for issue in proof['issues'])
    assert not (run / 'outcome.json').exists()
    assert not any(r.get('action') in ('query', 'drop', 'apply') for r in data['records'])
    first = path.read_bytes()
    assert close_review_budget(run, **kwargs) == path
    assert path.read_bytes() == first


@pytest.mark.parametrize('damage', ['not_exhausted', 'outstanding', 'wrong_source',
                                  'changed_ledger', 'changed_archive', 'dirty_read',
                                  'missing_proof', 'existing_settlement', 'corrupt_settlement'])
def test_closeout_refuses_unproved_or_conflicting_state(case, damage):
    run, kwargs, events, pending = case
    if damage in ('not_exhausted', 'outstanding', 'wrong_source', 'changed_ledger'):
        if damage == 'not_exhausted':
            for event in events:
                event['budget']['max_calls'] = 3
        elif damage == 'outstanding':
            events.pop()
        elif damage == 'wrong_source':
            events[0]['source_sha256'] = 'other'
        else:
            events[0]['extra'] = 'changed after validation'
        kwargs['ledger_path'].write_text(''.join(json.dumps(e) + '\n' for e in events))
        if damage != 'changed_ledger':
            kwargs['expected_ledger_sha256'] = sha(kwargs['ledger_path'])
    elif damage == 'changed_archive':
        next((kwargs['initial_coverage_path'].parent / 'coverage').rglob('finished_walk.json')).write_text('{}')
    elif damage in ('dirty_read', 'missing_proof'):
        path = run / 'finished_walk.json'
        payload = json.loads(path.read_text())
        if damage == 'dirty_read':
            payload['unverified_paragraphs'] = ['body-0000']
        else:
            payload.pop('verification_provenance')
        path.write_text(json.dumps(payload))
    else:
        (run / 'settlement.json').write_text('{}' if damage == 'existing_settlement' else '{bad')
    before = protected(case)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        close_review_budget(run, **kwargs)
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('damage', ['open_rows', 'findings', 'document', 'round'])
def test_idempotence_rejects_changed_closeout_or_inputs(case, damage):
    run, kwargs, *_ = case
    path = close_review_budget(run, **kwargs)
    if damage == 'open_rows':
        payload = json.loads(path.read_text())
        payload['open'].pop()
        path.write_text(json.dumps(payload))
    elif damage == 'findings':
        path_findings = run / 'findings.json'
        payload = json.loads(path_findings.read_text())
        payload['findings'].pop()
        path_findings.write_text(json.dumps(payload))
    elif damage == 'document':
        document = verify.deliverable_docx(run)
        document.write_bytes(document.read_bytes() + b'changed')
    else:
        kwargs['round_no'] = 1
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Existing settlement differs'):
        close_review_budget(run, **kwargs)
    assert path.read_bytes() == before


@pytest.mark.parametrize('status', ['error', 'started'])
def test_historical_terminal_unknown_is_preserved_but_active_call_blocks(case, status):
    run, kwargs, events, pending = case
    events.append({'source_sha256': 's' * 64, 'config_sha256': 'earlier-config',
                   'receipt_id': 'historical-book-call', 'operation_id': 'historical-book-call',
                   'model': 'fake', 'group': 'book', 'status': status, 'usage': None,
                   'reservation_output_tokens': 12000, 'reused': False})
    kwargs['ledger_path'].write_text(''.join(json.dumps(e) + '\n' for e in events))
    kwargs['expected_ledger_sha256'] = sha(kwargs['ledger_path'])
    if status == 'started':
        with pytest.raises(ValueError, match='Outstanding'):
            close_review_budget(run, **kwargs)
        assert not (run / 'settlement.json').exists()
    else:
        data = json.loads(close_review_budget(run, **kwargs).read_text())
        book = data['convergence']['resource_budget_closeout']['binding']['all_groups']['book']
        assert book['unknown_output_attempts'] == 1
        assert book['reserved_output_tokens'] == 12000


def test_full_coverage_is_revalidated_before_writing(case):
    run, kwargs, *_ = case
    kwargs['validate_current_coverage'] = lambda: False
    with pytest.raises(ValueError, match='full reading coverage'):
        close_review_budget(run, **kwargs)
    assert not (run / 'settlement.json').exists()


def test_exact_original_rows_share_astra_issue_identity(case):
    from galley.astra_review import build_packet
    run, kwargs, *_ = case
    original = json.loads((run / 'finished_walk.json').read_text())['residuals'][0]
    closeout = json.loads(close_review_budget(run, **kwargs).read_text())
    matching = [row for row in closeout['open'] if row == original]
    assert len(matching) == 1
    packet = build_packet(run)
    issues = [issue for issue in packet['issue_index'] if any(
        source['artifact'] == 'finished_walk.json' and source['index'] == 0
        for source in issue['sources'])]
    assert len(issues) == 1
    sources = {(source['artifact'], source['collection']) for source in issues[0]['sources']}
    assert {('finished_walk.json', 'residuals'), ('settlement.json', 'open'),
            ('settlement.json', 'residuals_seen')} <= sources


def test_astra_packet_projects_only_machine_budget_audit_with_full_digest_binding(case):
    from galley.astra_review import build_packet, _hash, _project_budget_closeout
    run, kwargs, *_ = case
    path = close_review_budget(run, **kwargs)
    first = build_packet(run)
    data = json.loads(path.read_text())
    audit = data['convergence']['resource_budget_closeout']
    audit['binding']['protected_files'].update({
        f'/machine-only-audit-path/{i}': 'a' * 64 for i in range(2000)})
    audit['issues'] *= 200
    path.write_text(json.dumps(data))
    raw = path.read_bytes()
    packet = build_packet(run)
    projected = packet['artifacts']['settlement.json']
    summary = projected['convergence']['resource_budget_closeout']
    assert path.read_bytes() == raw
    assert len(json.dumps(summary)) < 1000
    assert 'protected_files' not in summary and 'issues' not in summary
    assert '/machine-only-audit-path/' not in json.dumps(packet)
    assert summary['audit_sha256'] == _hash(audit)
    assert summary['binding_sha256'] == _hash(audit['binding'])
    assert summary['review_budget'] == audit['binding']['review_budget']
    assert summary['incomplete_round'] == 1
    for key in ('open', 'residuals_seen', 'notes'):
        assert projected[key] == data[key]
    assert {k: v for k, v in projected['convergence'].items() if k != 'resource_budget_closeout'} == {
        k: v for k, v in data['convergence'].items() if k != 'resource_budget_closeout'}
    assert packet['issue_index'] == first['issue_index']
    assert packet['packet_sha256'] != first['packet_sha256']
    assert summary['audit_sha256'] != first['artifacts']['settlement.json']['convergence']['resource_budget_closeout']['audit_sha256']
    ordinary = copy.deepcopy(data)
    ordinary['convergence']['stopped'] = 'rounds'
    assert _project_budget_closeout(ordinary) == ordinary
