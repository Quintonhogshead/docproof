"""Regression cases from the seven-file desktop launch acceptance."""
import pytest

from docproof.interior.astra import InteriorAstraError, _plan_prompt, _validate_plan


def seven_file_plan():
    pairs = [('beautful', 'beautiful'), ('freind', 'friend'), ('recieve', 'receive'),
             ('seperate', 'separate'), ('begining', 'beginning'),
             ('tommorow', 'tomorrow'), ('finaly', 'finally')]
    packet = {'sources': [], 'evidence': []}
    plan = {'instructions': [], 'edits': [], 'questions': [], 'designer_reasons': []}
    for index, (before, after) in enumerate(pairs):
        source, evidence, edit = f'source-{index}', f'evidence-{index}', f'edit-{index}'
        packet['sources'].append({'id': source, 'kind': 'docx'})
        packet['evidence'].append({'id': evidence, 'source_id': source,
                                   'kind': 'docx_paragraph', 'text': f'{before} to {after}'})
        plan['instructions'].append({'id': f'instruction-{index}', 'source_ids': [source],
            'covered_evidence_ids': [evidence], 'disposition': 'edit',
            'reason': 'Explicit spelling correction.', 'edit_ids': [edit]})
        plan['edits'].append({'id': edit, 'story_id': 'story', 'find': before,
            'replacement': after, 'expected_count': 1, 'font_style': '', 'style_ranges': []})
    packet['sources'].append({'id': 'empty-notes', 'kind': 'text', 'text': ''})
    packet['evidence'].append({'id': 'empty-evidence', 'source_id': 'empty-notes',
                               'kind': 'text', 'text': ''})
    packet['text_source_id'] = 'empty-notes'
    plan['instructions'].append({'id': 'no-notes', 'source_ids': ['empty-notes'],
        'covered_evidence_ids': [], 'disposition': 'already_correct',
        'reason': 'No optional notes supplied.', 'edit_ids': []})
    snapshot = {'stories': [{'id': 'story', 'text': ' '.join(p[0] for p in pairs)}]}
    return packet, snapshot, plan


def test_seven_corrections_and_empty_notes_need_no_clarification(tmp_path):
    packet, snapshot, plan = seven_file_plan()
    assert _validate_plan(plan, packet, snapshot) == plan
    prompt = _plan_prompt(packet, snapshot, None, tmp_path/'packet.json', tmp_path/'snapshot.json')
    assert 'font_style="" and style_ranges=[]' in prompt
    assert 'never offsets in the full story' in prompt
    assert 'needs no clarification' in prompt
    assert 'edit_ids=[] and covered_evidence_ids=[]' in prompt


@pytest.mark.parametrize('fault, expected', [
    ('empty', 'missing=0, duplicates=0, context_only=1'),
    ('missing', 'missing=1, duplicates=0, context_only=0'),
    ('duplicate', 'missing=0, duplicates=1, context_only=0'),
])
def test_coverage_guards_still_distinguish_real_omissions(fault, expected):
    packet, snapshot, plan = seven_file_plan()
    if fault == 'empty':
        plan['instructions'][-1]['covered_evidence_ids'] = ['empty-evidence']
    elif fault == 'missing':
        plan['instructions'][0]['covered_evidence_ids'] = []
    else:
        plan['instructions'][0]['covered_evidence_ids'] *= 2
    with pytest.raises(InteriorAstraError, match=expected):
        _validate_plan(plan, packet, snapshot)


def test_story_style_coordinates_are_rejected_but_replacement_spans_are_valid():
    packet, snapshot, plan = seven_file_plan()
    plan['edits'][0]['style_ranges'] = [{'start': 55, 'end': 63, 'font_style': 'Italic'}]
    with pytest.raises(InteriorAstraError, match='invalid style range'):
        _validate_plan(plan, packet, snapshot)
    plan['edits'][0]['style_ranges'] = [{'start': 0, 'end': 9, 'font_style': 'Italic'}]
    assert _validate_plan(plan, packet, snapshot) == plan
