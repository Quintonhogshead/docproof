from copy import deepcopy
import json

import pytest

import galley.codex_runner as runner
from galley.astra_review import AstraReviewError
from docproof.interior.astra import InteriorAstraError, PLAN_SCHEMA
from docproof.interior.routing import LunaFirstReviewer


def inputs():
    packet = {'sources': [{'id': 'source', 'kind': 'text', 'text': 'Fix teh; italicize word.'}],
              'text_source_id': 'source', 'evidence': [
                  {'id': 'typo', 'source_id': 'source', 'kind': 'text', 'text': 'teh to the'},
                  {'id': 'style', 'source_id': 'source', 'kind': 'text', 'text': 'Italicize word'}]}
    snapshot = {'stories': [{'id': 'story', 'text': 'A teh word.', 'style_ranges': [], 'pages': [1]}]}
    return packet, snapshot


def instruction(evidence, disposition='edit', edit_ids=None):
    return {'id': evidence, 'source_ids': ['source'], 'disposition': disposition, 'reason': evidence,
            'covered_evidence_ids': [evidence], 'edit_ids': edit_ids if edit_ids is not None else [evidence]}


def edit(eid='typo', find='teh', replacement='the', style=''):
    return {'id': eid, 'story_id': 'story', 'find': find, 'replacement': replacement,
            'expected_count': 1, 'font_style': style, 'style_ranges': []}


def plan(instructions, edits):
    return {'instructions': instructions, 'edits': edits, 'questions': [], 'designer_reasons': []}


def test_simple_plan_is_luna_but_final_review_stays_astra(monkeypatch, tmp_path):
    packet, snapshot = inputs()
    packet['evidence'] = packet['evidence'][:1]
    calls = []
    def fake(prompt, schema, work_dir, *, request_id, **options):
        calls.append(options)
        if schema is PLAN_SCHEMA:
            return plan([instruction('typo')], [edit()])
        return {'status': 'verified', 'instruction_ids': ['typo'], 'reviewed_pages': [1], 'reasons': []}
    monkeypatch.setattr(runner, 'run_structured', fake)
    reviewer = LunaFirstReviewer()
    result = reviewer.plan(packet, snapshot, tmp_path)
    reviewer.review(packet, snapshot, {'instructions': result['instructions'], 'required_review_pages': [1]},
                    result['edits'], tmp_path)
    assert calls == [{'model': 'gpt-5.6-luna', 'reasoning_effort': 'medium'}, {}]
    summary = json.loads((tmp_path / 'astra-review-summary.json').read_text('utf-8'))
    assert summary['edits'] == result['edits']
    assert 'stories' not in summary


@pytest.mark.parametrize('luna_style_edit', [False, True])
def test_only_complex_evidence_is_escalated_and_merged(monkeypatch, tmp_path, luna_style_edit):
    packet, snapshot = inputs()
    original = deepcopy(packet)
    calls = []
    def fake(prompt, schema, work_dir, *, request_id, **options):
        calls.append(options)
        if options:
            rows = [instruction('typo'), instruction('style', 'edit' if luna_style_edit else 'designer',
                                                     ['style'] if luna_style_edit else [])]
            return plan(rows, [edit()] + ([edit('style', 'word', 'word', 'Italic')] if luna_style_edit else []))
        subset = json.loads((work_dir / 'astra-interior-packet.json').read_text('utf-8'))
        assert [row['id'] for row in subset['evidence']] == ['style']
        assert 'only' in prompt.lower() and 'packet.assignment' in prompt
        # Deliberately reuse a Luna edit ID; the router must namespace both.
        return plan([instruction('style', edit_ids=['typo'])], [edit('typo', 'word', 'word', 'Italic')])
    monkeypatch.setattr(runner, 'run_structured', fake)
    result = LunaFirstReviewer().plan(packet, snapshot, tmp_path)
    assert [row['id'] for row in result['edits']] == ['luna-typo', 'astra-typo']
    assert [row['find'] for row in result['edits']] == ['teh', 'word']
    assert packet == original
    assert len(calls) == 2
    receipt = json.loads((tmp_path / 'model-routing.json').read_text('utf-8'))
    assert receipt['escalated_evidence_ids'] == ['style'] and not receipt['full_escalation']


def test_invalid_anchor_routes_completed_proposal_to_astra_once(monkeypatch, tmp_path):
    packet, snapshot = inputs()
    packet['evidence'] = packet['evidence'][:1]
    calls = []
    def fake(prompt, schema, work_dir, *, request_id, **options):
        calls.append(options)
        return plan([instruction('typo')], [edit(find='absent' if options else 'teh')])
    monkeypatch.setattr(runner, 'run_structured', fake)
    result = LunaFirstReviewer().plan(packet, snapshot, tmp_path)
    assert result['edits'][0]['find'] == 'teh' and len(calls) == 2
    assert json.loads((tmp_path / 'model-routing.json').read_text('utf-8'))['full_escalation']


def test_operational_failure_does_not_trigger_another_model(monkeypatch, tmp_path):
    calls = []
    def fake(*args, **kwargs):
        calls.append(kwargs)
        raise AstraReviewError('subscription review stopped (quota)')
    monkeypatch.setattr(runner, 'run_structured', fake)
    with pytest.raises(AstraReviewError, match='quota'):
        LunaFirstReviewer().plan(*inputs(), tmp_path)
    assert len(calls) == 1


def test_escalation_cannot_modify_unassigned_correction(monkeypatch, tmp_path):
    def fake(prompt, schema, work_dir, *, request_id, **options):
        if options:
            return plan([instruction('typo'), instruction('style', 'designer', [])], [edit()])
        return plan([instruction('typo')], [edit()])
    monkeypatch.setattr(runner, 'run_structured', fake)
    with pytest.raises(InteriorAstraError, match='unknown evidence'):
        LunaFirstReviewer().plan(*inputs(), tmp_path)


def test_shared_evidence_group_escalates_accepted_and_designer_rows_together(monkeypatch, tmp_path):
    packet, snapshot = inputs()
    packet['evidence'] = packet['evidence'][:1]
    calls = []

    def fake(prompt, schema, work_dir, *, request_id, **options):
        calls.append(options)
        style_row = instruction('style', 'designer', [])
        style_row['covered_evidence_ids'] = []
        if len(calls) == 1:
            return plan(
                [instruction('typo'), style_row],
                [edit()])
        subset = json.loads((work_dir / 'astra-interior-packet.json').read_text('utf-8'))
        assert [row['id'] for row in subset['evidence']] == ['typo']
        return plan(
            [instruction('typo'), style_row],
            [edit()])

    monkeypatch.setattr(runner, 'run_structured', fake)
    result = LunaFirstReviewer().plan(packet, snapshot, tmp_path)
    assert [row['id'] for row in result['instructions']] == ['astra-typo', 'astra-style']
    assert [row['id'] for row in result['edits']] == ['astra-typo']
    assert len(calls) == 2


def test_shared_group_keeps_unrelated_complex_source_and_two_astra_edits(monkeypatch, tmp_path):
    packet, snapshot = inputs()
    packet['evidence'] = packet['evidence'][:1]
    packet['sources'].append({'id': 'source-2', 'kind': 'text', 'text': 'Other passage.'})
    packet['evidence'].append({'id': 'other', 'source_id': 'source-2',
                               'kind': 'text', 'text': 'Format other'})
    calls = []

    def fake(prompt, schema, work_dir, *, request_id, **options):
        calls.append(options)
        style_row = instruction('style', 'designer', [])
        style_row['covered_evidence_ids'] = []
        other_row = instruction('other', 'edit', ['other'])
        other_row['source_ids'] = ['source-2']
        if len(calls) == 1:
            return plan([instruction('typo'), style_row, other_row],
                        [edit(), edit('other', 'word', 'word', 'Italic')])
        subset = json.loads((work_dir / 'astra-interior-packet.json').read_text('utf-8'))
        assert {row['id'] for row in subset['evidence']} == {'typo', 'other'}
        return plan([instruction('typo'), style_row, other_row],
                    [edit(), edit('other', 'word', 'word', 'Italic')])

    monkeypatch.setattr(runner, 'run_structured', fake)
    result = LunaFirstReviewer().plan(packet, snapshot, tmp_path)
    assert [row['id'] for row in result['edits']] == ['astra-typo', 'astra-other']
    assert all(row['id'].startswith('astra-') for row in result['instructions'])
    assert len(calls) == 2
