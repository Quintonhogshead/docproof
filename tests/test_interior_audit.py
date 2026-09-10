import copy
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from docproof.interior.audit import build_audit, write_audit
from docproof.interior.workflow import save_json


def seed(work, *, stage='complete', text='A corrected sentence.', outcome='verified'):
    packet = {'sources': [{'id': 's1', 'name': 'corrections.txt', 'kind': 'file', 'sha256': 'abc'}],
              'evidence': [{'id': 'v1', 'source_id': 's1', 'kind': 'text', 'text': 'Change the sentence.'}]}
    plan = {'instructions': [{'id': 'i1', 'source_ids': ['s1'], 'covered_evidence_ids': ['v1'],
                            'disposition': 'edit', 'edit_ids': ['e1'], 'reason': 'Requested change'}],
            'edits': [{'id': 'e1', 'story_id': '1', 'find': 'A sentence.', 'replacement': 'A corrected sentence.',
                       'expected_count': 1}], 'questions': [], 'designer_reasons': []}
    baseline = {'stories': [{'id': '1', 'text': 'A sentence.', 'style_ranges': []}], 'fonts': [], 'links': [], 'overset': []}
    final = copy.deepcopy(baseline)
    final['stories'][0]['text'] = text
    final['required_review_pages'] = [1]
    for name, value in [('packet', packet), ('plan', plan), ('baseline', baseline), ('final', final),
                        ('submission', {'source': 'Author - Book 4.indd', 'attachments': [], 'text': 'Notes'}),
                        ('workflow', {'stage': stage}), ('applied', {}),
                        ('review', {'status': outcome, 'instruction_ids': ['i1'], 'reviewed_pages': [1], 'reasons': []})]:
        save_json(work / (name + '.json'), value)
    return {'status': outcome, 'reasons': [], 'output_indd': ''}


@pytest.mark.parametrize('stage,applied,text,status,expected', [
    ('complete', True, 'A corrected sentence.', 'verified', 'Done — verified'),
    ('reviewing', True, 'A corrected sentence.', 'technical_block', 'Applied — book review required'),
    ('applying', True, 'A corrected sentence.', 'technical_block', 'Unconfirmed — apply interrupted'),
    ('applying', False, 'A corrected sentence.', 'technical_block', 'Unconfirmed — apply interrupted'),
    ('before apply', False, 'A corrected sentence.', 'planned', 'Not done — not applied'),
    ('applied', True, 'Wrong text.', 'technical_block', 'Unconfirmed — saved text not verified'),
])
def test_status_requires_saved_evidence(tmp_path, stage, applied, text, status, expected):
    result = seed(tmp_path, stage=stage, text=text, outcome=status)
    if not applied:
        (tmp_path / 'applied.json').unlink()
    data = build_audit(tmp_path, result)
    assert data['corrections'][0]['Status'] == expected
    if 'Unconfirmed' in expected or 'Not done' in expected:
        assert data['changes'][0]['Confirmed occurrences'] is None


def test_fonts_do_not_erase_text_proof_or_claim_book_passed(tmp_path):
    result = seed(tmp_path, outcome='designer_needed')
    final = json.loads((tmp_path / 'final.json').read_text())
    final['fonts'] = [{'name': 'Missing', 'status': 'NOT_AVAILABLE'}]
    save_json(tmp_path / 'final.json', final)
    data = build_audit(tmp_path, result)
    assert data['corrections'][0]['Status'] == 'Applied — book review required'
    assert data['changes'][0]['Text confirmed'] == 'Yes'
    assert any('Unavailable font' in str(row) for row in data['details'])


def test_incomplete_review_cannot_claim_done(tmp_path):
    result = seed(tmp_path)
    save_json(tmp_path / 'review.json', {'status': 'verified', 'instruction_ids': ['i1'], 'reviewed_pages': []})
    assert build_audit(tmp_path, result)['corrections'][0]['Status'] != 'Done — verified'


def test_invalid_omitted_evidence_visible_as_remaining_work(tmp_path):
    result = seed(tmp_path)
    packet = json.loads((tmp_path / 'packet.json').read_text())
    packet['evidence'].append({'id': 'v2', 'source_id': 's1', 'kind': 'text', 'text': 'Forgotten change'})
    save_json(tmp_path / 'packet.json', packet)
    data = build_audit(tmp_path, result)
    assert data['instruction_count'] is None
    assert data['corrections'][0]['Status'] == 'Unconfirmed — invalid plan'
    assert data['corrections'][1]['Requested correction / source wording'] == 'Forgotten change'
    assert data['evidence'][1]['Coverage'] == 'Uncovered'


def test_preplan_failure_preserves_seven_slots_duplicates_and_notes(tmp_path):
    job = {'submission_urls': ['private-url'] * 7,
           'submission_text': 'Fix all seven files.',
           'attachment_receipts': {'0': {'filename': 'first.txt', 'sha256': 'abc'},
                                   '1': {'filename': 'same.txt', 'sha256': 'abc', 'duplicate_of': '0'}}}
    data = build_audit(tmp_path, {'status': 'technical_block'}, context=job)
    assert len(data['files']) == 7
    assert data['files'][1]['Duplicate of slot'] == 1
    assert data['files'][6]['Receipt'] == 'Missing / not read'
    assert data['instruction_count'] is None
    assert 'private-url' not in json.dumps(data)
    assert data['corrections'][0]['Requested correction / source wording'] == 'Fix all seven files.'


def test_corrupt_plan_and_packet_still_produce_a_blocked_ledger(tmp_path):
    (tmp_path / 'packet.json').write_text('broken')
    (tmp_path / 'plan.json').write_text('[]')
    data = build_audit(tmp_path, {'status': 'technical_block', 'submitted_attachments': [{'name': 'unread.docx'}]})
    assert data['corrections'][0]['Status'] == 'Not done — blocked before planning'
    assert data['files'][0]['Filename'] == 'unread.docx'
    assert any('packet.json could not' in row['Result'] for row in data['details'])


def test_changed_completed_artifact_invalidates_saved_text_proof(tmp_path):
    result = seed(tmp_path)
    output = tmp_path / 'Author - Book 4.5.indd'
    output.write_bytes(b'changed')
    save_json(tmp_path / 'workflow.json', {'stage': 'complete', 'artifact_hashes': {str(output): 'old-hash'}})
    data = build_audit(tmp_path, {'status': 'technical_block'})
    assert data['corrections'][0]['Status'] == 'Unconfirmed — saved text not verified'
    assert data['changes'][0]['Confirmed occurrences'] is None
    assert any('Saved-change proof is no longer current' in row['Result'] for row in data['details'])


@pytest.mark.parametrize('payload', [
    '=HYPERLINK("https://example.invalid","do not execute") ' + 'Long requested wording. ' * 1600,
    '2026-09-10T16:19:01+00:00',
    '09/10/2026',
    '000123',
], ids=['long-literal', 'iso-date', 'date', 'leading-zeros'])
def test_xlsx_exact_source_text_long_rows_counts_and_no_executable_input(tmp_path, payload):
    result = seed(tmp_path)
    packet = json.loads((tmp_path / 'packet.json').read_text())
    packet['evidence'][0]['text'] = payload
    save_json(tmp_path / 'packet.json', packet)
    output = write_audit(tmp_path, result)
    ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(output) as archive:
        root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        strings = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        shared = [''.join(n.itertext()) for n in strings]
        cells = {c.attrib['r']: c for c in root.findall('.//x:sheetData/x:row/x:c', ns)}
        def value(cell):
            if cell.get('t') == 'inlineStr':
                return ''.join(cell.find('x:is', ns).itertext())
            v = cell.find('x:v', ns)
            return shared[int(v.text)] if cell.get('t') == 's' else v.text if v is not None else ''
        rows = sorted(int(addr[1:]) for addr in cells if addr.startswith('C') and int(addr[1:]) >= 10)
        assert ''.join(value(cells[f'C{r}']) for r in rows) == payload
        assert value(cells['G4']) == '1'
        assert value(cells['G6']) == '0'
        for r in rows:
            assert cells[f'C{r}'].find('x:f', ns) is None
            assert cells[f'C{r}'].get('t') == 'inlineStr'
        assert root.find('.//x:pane', ns).get('topLeftCell') == 'C10'
        assert archive.testzip() is None


def test_report_failure_does_not_escape_as_verified(tmp_path, monkeypatch):
    from docproof.interior.workflow import run_local
    def fail(*args, **kwargs):
        raise RuntimeError('Renderer unavailable')
    monkeypatch.setattr('docproof.interior.audit.write_audit', fail)
    source = tmp_path / 'Author - Book 4.indd'
    source.write_bytes(b'source')
    # Failure before native boundaries must still produce a durable technical receipt.
    result = run_local(source, [], '', tmp_path / 'job')
    assert result['status'] == 'technical_block'
    assert 'audit_spreadsheet' not in result
    assert (tmp_path / 'job/technical-block.json').is_file()
