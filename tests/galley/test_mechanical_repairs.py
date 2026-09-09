"""Regression cases from Bradshaw: corrections, recovery, and comment cleanup."""
import json

import pytest

from galley.comment_reconcile import actual_comments, reconciliation
from galley.manifest import _certify_settlement, _certify_comment_premises
from galley.outcome import assess
from galley.settle import Settler, SettleOptions, _fact, rewrite_class, open_items
from galley.verify import deliverable_docx, residual_id
from docproof.config import load_config
from tests.galley.test_settle import (
    _manuscript, _para_ids, _build, _walk, _settle, _accepted, _records,
    _replay_config, _Provider,
)


@pytest.mark.parametrize('before,after', [
    ('My stomach sunk', 'My stomach sank'),
    ('She says she were here', 'She said she was here'),
    ('day 2', 'day two'), ('at seven PM', 'at 7:00 PM'),
    ('With WIFI', 'With Wi-Fi'), ('U.S Army', 'U.S. Army'),
    ('He told Hunter and I to be a real man', 'He told Hunter and me to be real men'),
])
def test_mechanical_candidates_are_not_rewrites(before, after):
    assert rewrite_class(before, after) is None
    assert _fact(before, after) is None


def test_actual_number_change_still_needs_judgment():
    assert _fact('day two', 'day three')
    assert _fact('at 7:00 PM', 'at 8:00 PM')
    assert rewrite_class('He agreed', 'He refused')
    assert _fact('The dose was 1.5 units.', 'The dose was 15 units.')
    assert _fact('It was -2 degrees.', 'It was 2 degrees.')
    assert _fact('From 2 to 3.', 'From 3 to 2.')


def test_bradshaw_fixes_land_and_repeated_run_is_idempotent(tmp_path):
    paras = ['He joined the U.S Army and met U.S agents.', 'My stomach sunk.',
             'He returned on day 2.', 'The file is CONFIDENTAL.']
    src = _manuscript(tmp_path, paras)
    ids, _ = _para_ids(src)
    run = _build(tmp_path, src, [dict(para_id=ids[3], original_text='CONFIDENTAL',
                                    corrected_text='CONFIDENTIAL', confidence='high')])
    _walk(run, [dict(para_id=ids[1], quote='My stomach sunk', suggestion='My stomach sank',
                    problem='Wrong past tense'),
                dict(para_id=ids[2], quote='day 2', suggestion='day two', problem='House number style')])
    assert _settle(tmp_path, run, src, '--mechanical-only') == 0
    first = _accepted(run)
    assert first[ids[0]] == 'He joined the U.S. Army and met U.S. agents.'
    assert first[ids[1]] == 'My stomach sank.'
    assert first[ids[2]] == 'He returned on day two.'
    assert not actual_comments(deliverable_docx(run))
    assert _settle(tmp_path, run, src, '--mechanical-only') == 0
    assert _accepted(run) == first


def test_internal_failure_survives_restart_and_blocks_certification(tmp_path):
    src = _manuscript(tmp_path)
    ids, _ = _para_ids(src)
    run = _build(tmp_path, src, [dict(para_id=ids[0], original_text='teh', corrected_text='the')])
    _walk(run, [dict(para_id=ids[1], quote='cannot locate this', suggestion='receive', problem='Spelling')])
    assert _settle(tmp_path, run, src) == 1
    records, settlement = _records(run)
    assert settlement.open and next(iter(records.values())).action == 'internal_repair'
    assert not actual_comments(deliverable_docx(run))
    assert _certify_settlement(run).status == 'fail'
    assert assess(run).outcome == 'needs_human'
    assert 'internal repair' in assess(run).reason
    # A later verification snapshot must not erase the unresolved repair.
    _walk(run, [])
    assert len(open_items(run)) == 1
    assert _settle(tmp_path, run, src) == 1
    assert _records(run)[1].open


def test_judge_can_apply_multiword_grammar_or_ask_a_specific_question(tmp_path):
    src = _manuscript(tmp_path, ['He busying the machine.', 'Alex met Jordan on Tuesday.'])
    ids, _ = _para_ids(src)
    run = _build(tmp_path, src, [dict(para_id=ids[1], original_text='Tuesday', corrected_text='Tuesday',
                                    force_query=True, explanation='Which Tuesday?')])
    _walk(run, [dict(para_id=ids[0], quote='He busying', suggestion='He was busy with', problem='Dropped words'),
                dict(para_id=ids[1], quote='Alex', suggestion='Sam', problem='Two plausible identities')])
    provider = _Provider(
        dict(action='add', replacement='He was busy with', category='grammar',
             preserves_meaning=True, reason='Restore the missing verb and preposition'),
        dict(action='query', question='Was it Alex or Sam who met Jordan?',
             missing_knowledge='The identity of the person who met Jordan'))
    result = Settler(run, cfg=load_config(_replay_config(tmp_path)), manuscript=src,
                     error_dir='config/error_types', provider=provider,
                     options=SettleOptions(mechanical_only=True, verify_delta=False)).run()
    assert not result.open
    assert _accepted(run)[ids[0]] == 'He was busy with the machine.'
    comments = actual_comments(deliverable_docx(run))
    assert any('Alex or Sam' in c['explanation'] for c in comments)
    assert not any('Dropped words' in c['explanation'] for c in comments)


def test_stale_and_duplicate_queries_removed_but_source_comment_preserved(tmp_path):
    import docx
    source = docx.Document()
    para = source.add_paragraph('CONFIDENTAL MEDICAL RECORD')
    source.add_comment(para.runs, text='Author note: keep this heading.', author='Author')
    src = tmp_path / 'book.docx'
    source.save(src)
    ids, _ = _para_ids(src)
    rows = [dict(para_id=ids[0], original_text='CONFIDENTAL', corrected_text='CONFIDENTIAL'),
            dict(para_id=ids[0], original_text='CONFIDENTAL MEDICAL RECORD',
                 corrected_text='CONFIDENTIAL MEDICAL RECORD', force_query=True,
                 explanation='Missing letter')]
    run = _build(tmp_path, src, rows)
    _walk(run, [])
    assert _settle(tmp_path, run, src) == 0
    comments = actual_comments(deliverable_docx(run))
    assert len(comments) == 1
    assert comments[0]['author'] == 'Author'
    assert _accepted(run)[ids[0]] == 'CONFIDENTIAL MEDICAL RECORD'
    env = json.loads((run / 'findings.json').read_text())
    assert _certify_comment_premises(run, env).status == 'pass'
    receipt = json.loads((run / 'comment_reconciliation.json').read_text())
    assert receipt['comment_count'] == 1


def test_dedup_preserves_different_questions_in_same_sentence():
    rows = [dict(para_id='p', status='query', original_text='Alex met Sam.',
                 explanation=q, finding_id=str(i)) for i, q in enumerate([
                     'Which Alex?', 'Which Sam?', 'Which Alex?'])]
    removed = reconciliation(rows, {'p': 'Alex met Sam.'})
    assert len(removed) == 1


def test_overlapping_corrections_recover_without_losing_either_fix(tmp_path):
    src = _manuscript(tmp_path, ['My stomach sunk near the U.S Army base.'])
    ids, _ = _para_ids(src)
    run = _build(tmp_path, src, [])
    _walk(run, [dict(para_id=ids[0], quote='My stomach sunk',
                    suggestion='My stomach sank', problem='Wrong past tense')])
    assert _settle(tmp_path, run, src, '--mechanical-only') == 0
    assert _accepted(run)[ids[0]] == 'My stomach sank near the U.S. Army base.'
    assert not actual_comments(deliverable_docx(run))


def test_persistent_rebuild_failure_preserves_other_successes(tmp_path, monkeypatch):
    src = _manuscript(tmp_path, ['My stomach sunk and I recieve letters.'])
    ids, _ = _para_ids(src)
    run = _build(tmp_path, src, [])
    _walk(run, [dict(para_id=ids[0], quote='My stomach sunk', suggestion='My stomach sank',
                    problem='Wrong past tense'),
                dict(para_id=ids[0], quote='recieve', suggestion='receive', problem='Spelling')])
    real = Settler._self_check

    def fail_one(self, plans, unplannable, failed, records, em):
        bad = real(self, plans, unplannable, failed, records, em)
        if any(r.residual_id == residual_id(ids[0], 'recieve') for r in records):
            bad[ids[0]] = 'composite_mismatch'
        return bad
    monkeypatch.setattr(Settler, '_self_check', fail_one)
    assert _settle(tmp_path, run, src, '--mechanical-only') == 1
    assert _accepted(run)[ids[0]] == 'My stomach sank and I recieve letters.'
    assert not actual_comments(deliverable_docx(run))
    assert len(_records(run)[1].open) == 1
    assert _certify_settlement(run).status == 'fail'


def test_correction_elsewhere_does_not_remove_a_live_query():
    row = dict(finding_id='q1', status='query', para_id='p', occurrence=2,
               original_text='CONFIDENTAL', corrected_text='CONFIDENTIAL')
    source = {'p': 'CONFIDENTAL first; CONFIDENTAL second.'}
    delivered = {'p': 'CONFIDENTIAL first; CONFIDENTAL second.'}
    assert not reconciliation([row], delivered, source=source)


def test_query_requires_specific_missing_author_knowledge():
    from galley.settle import Residual, judge_decision
    from docproof.providers import ProviderResult
    res = Residual('r', 'residual', 'p', 'U.S', 'missing period', 'U.S.', 'high')
    decision = judge_decision(res, ProviderResult(stop_reason='ok', parsed={
        'action': 'query', 'question': 'Please confirm this correction.'}))
    assert decision.action == 'internal_repair'


def test_comment_receipt_is_invalid_after_document_changes(tmp_path):
    src = _manuscript(tmp_path, ['The U.S Army arrived.'])
    run = _build(tmp_path, src, [])
    _walk(run, [])
    assert _settle(tmp_path, run, src) == 0
    path = deliverable_docx(run)
    # Even a metadata/package change invalidates the final-build receipt.
    path.write_bytes(path.read_bytes() + b'changed')
    env = json.loads((run / 'findings.json').read_text())
    assert _certify_comment_premises(run, env).status == 'fail'


def test_legacy_abbreviation_query_without_suggestion_becomes_a_fix(tmp_path):
    src = _manuscript(tmp_path, ['He joined the U.S Army.'])
    ids, _ = _para_ids(src)
    run = _build(tmp_path, src, [dict(para_id=ids[0], original_text='U.S Army',
        corrected_text='U.S Army', force_query=True,
        explanation='Missing period in abbreviation')])
    _walk(run, [])
    assert _settle(tmp_path, run, src, '--mechanical-only') == 0
    assert _accepted(run)[ids[0]] == 'He joined the U.S. Army.'
    assert not actual_comments(deliverable_docx(run))


def test_recovery_never_overwrites_a_conflicting_earlier_edit():
    from galley.mechanics import rebase_correction
    assert rebase_correction('Alex met Sam.', 'Alex met Jordan.', 'Sam', 'Lee') is None
    assert rebase_correction('My stomach sunk.', 'My stomach sank.',
                             'My stomach sunk', 'My stomach sank') == 'My stomach sank.'
