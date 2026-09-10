from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.watch import native_corrections as native, native_intake as intake, native_queue as queue
from app.watch.drive import DriveFile
from app.watch.settings import WatchSettings
from app.watch.tick import TickReport
from docproof.interior.book_identity import verify_identity


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(intake.time, 'time', lambda: clock[0])
    book = dict(project_id='project', title='The Book', author='Bill Sibley', surname='Sibley',
                folder_id='interior', source_id='source', source_version=4,
                title_aliases=[], author_aliases=[])
    queue.register_book(tmp_path, book)
    ws = WatchSettings(corrections_enabled=True, corrections_engine='native',
                       corrections_native_form_poll=True, corrections_native_form_id='form',
                       corrections_native_start_after='2026-09-01',
                       corrections_native_form_project_property='project_id',
                       corrections_native_form_book_property='book', corrections_native_project_book_property='book',
                       corrections_native_project_first_property='first', corrections_native_project_last_property='last',
                       corrections_native_form_file_property='files', corrections_native_form_notes_property='notes',
                       corrections_native_auto_upload=True, hubspot_write_back=False)
    events, calls, uploads, missing, content = [], [], [], set(), {}
    files = [DriveFile('source', 'Sibley - Book 4.indd', 'application/octet-stream')]
    remote = {}
    monkeypatch.setattr(native, 'form_submissions', lambda *a, **kw: list(events))
    monkeypatch.setattr(native, '_project_records', lambda *a, **kw: [native.hubspot.HubSpotRecord(
        'project', {'first': 'Bill', 'last': 'Sibley', 'book': 'The Book'})])
    monkeypatch.setattr(native.drive, 'list_folder', lambda *a, **kw: list(files))
    def source_download(_token, _id, target, **kw):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'original')
        return target
    monkeypatch.setattr(native.drive, 'download', source_download)
    monkeypatch.setattr(native, '_assets', lambda *a, **kw: [])
    monkeypatch.setattr(native, '_asset_sibling_folders', lambda *a, **kw: [])
    monkeypatch.setattr(native.native_files, 'cached_file', lambda *a, **kw: None)
    def attachment(_token, url, target, **kw):
        if url in missing:
            raise native.native_files.ManualAttachmentRequired('12345', 'notes.txt')
        target.mkdir(parents=True, exist_ok=True)
        path = target / 'notes.txt'
        path.write_bytes(content.get(url, url.encode()))
        return path
    monkeypatch.setattr(native.native_files, 'download_file', attachment)
    def workflow(source, attachments, text, out, rules):
        calls.append(dict(attachments=list(attachments), text=text, rules=rules))
        out.mkdir(parents=True, exist_ok=True)
        for name, value in [('book.indd', b'corrected'), ('book.pdf', b'pdf'),
                            ('report.json', b'{}'), ('corrections.xlsx', b'xlsx')]:
            (out / name).write_bytes(value)
        return {'status': 'verified', 'output_indd': str(out / 'book.indd'),
                'output_pdf': str(out / 'book.pdf'), 'report': str(out / 'report.json'),
                'audit_spreadsheet': str(out / 'corrections.xlsx')}
    monkeypatch.setattr(native, '_call_workflow', workflow)
    def upload(_token, folder, path, *, name, app_properties, **kw):
        fid = 'out-' + str(len(uploads))
        body = path.read_bytes()
        uploads.append(name)
        files.append(DriveFile(fid, name, 'application/octet-stream', app_properties))
        remote[fid] = {'id': fid, 'name': name, 'parents': [folder], 'size': str(len(body)),
                       'appProperties': app_properties, 'md5Checksum': hashlib.md5(body).hexdigest()}
        return fid
    monkeypatch.setattr(native.drive, 'upload', upload)
    monkeypatch.setattr(native.drive, '_json_call', lambda req, **kw: remote[urlparse(req.full_url).path.rsplit('/', 1)[-1]])
    def add(eid, urls=(), note='', pid='project', title='The Book'):
        events.append({'conversionId': eid, 'submittedAt': int(clock[0] * 1000), 'values': [
            {'name': 'project_id', 'value': pid}, {'name': 'book', 'value': title},
            {'name': 'files', 'value': ';'.join(urls)}, {'name': 'notes', 'value': note},
            {'name': 'firstname', 'value': 'Nancy'}, {'name': 'lastname', 'value': 'Cook-Monroe'}]})
    def run():
        report = TickReport()
        native.run_stage('drive', tmp_path, ws, None, None, None, mock=False,
                         opener=lambda *_: pytest.fail('Unexpected network request'), hs_token='hs', report=report)
        return report
    return SimpleNamespace(**locals())


def test_seven_received_files_wait_three_hours_and_deliver_once(harness):
    h = harness
    for i in range(7):
        h.add(str(i), [f'https://files.test/{i}.txt'])
    h.run()
    assert len(queue.status(h.tmp_path)['events']) == 7 and not h.calls
    h.clock[0] += 10799
    h.run()
    assert not h.calls
    h.clock[0] += 1
    report = h.run()
    assert not report.failed and not report.needs_human
    assert len(h.calls) == 1 and len(h.calls[0]['attachments']) == 7
    assert len({str(p) for p in h.calls[0]['attachments']}) == 7
    job = native._read_jobs(h.tmp_path)[0]
    assert job['expected_attachment_count'] == job['received_attachment_count'] == 7
    assert len(job['submission_receipts']) == 7
    assert job['book_identity']['author'] == 'Bill Sibley'  # proxy submitter did not select the book
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'delivered'
    assert queue.books(h.tmp_path)['project']['source_version'] == 4.5
    h.run()
    assert len(h.calls) == 1 and len(h.uploads) == 4


def test_one_missing_file_blocks_all_edits_and_preserves_other_receipts(harness):
    h = harness
    urls = [f'https://files.test/{i}.txt' for i in range(7)]
    h.missing.add(urls[3])
    h.add('all', urls)
    h.run()
    h.clock[0] += 10800
    h.run()
    job = native._read_jobs(h.tmp_path)[0]
    assert not h.calls and not h.uploads
    assert job['expected_attachment_count'] == 7 and job['received_attachment_count'] == 6
    assert job['status'] == 'awaiting_attachment' and len(job['missing_attachments']) == 1


def test_duplicate_bytes_keep_every_receipt_but_analyze_once(harness):
    h = harness
    urls = ['https://files.test/a.txt', 'https://files.test/b.txt']
    h.content.update({url: b'fix this word' for url in urls})
    h.add('duplicates', urls)
    h.run()
    h.clock[0] += 10800
    h.run()
    job = native._read_jobs(h.tmp_path)[0]
    assert len(h.calls[0]['attachments']) == 1
    assert len(job['attachment_receipts']) == 2
    assert job['attachment_receipts']['1']['duplicate_of'] == '0'


@pytest.mark.parametrize('pid,title', [('', 'The Book'), ('other', 'The Book'), ('project', 'Wrong Book')])
def test_unknown_identity_or_conflicting_title_never_starts(harness, pid, title):
    h = harness
    h.add('bad', note='Fix it', pid=pid, title=title)
    h.run()
    h.clock[0] += 10800
    report = h.run()
    assert not h.calls and not h.uploads and report.needs_human


def test_unknown_submission_holds_other_new_batches_until_identified(harness):
    h = harness
    h.add('good', note='Fix it')
    h.add('unknown', note='Also this', pid='')
    h.run()
    h.clock[0] += 10800
    h.run()
    assert not h.calls


@pytest.mark.parametrize('version', [4, 5])
def test_tied_or_new_unregistered_source_holds(harness, version):
    h = harness
    h.add('book', note='Fix it')
    h.run()
    h.files.append(DriveFile('designer', f'Sibley - Book {version}.indd', 'application/octet-stream'))
    h.clock[0] += 10800
    h.run()
    assert not h.calls and queue.status(h.tmp_path)['batches'][0]['state'] == 'held'


def test_upload_failure_reuses_output_after_restart(harness, monkeypatch):
    h = harness
    upload = native.drive.upload
    attempts = [0]
    def flaky(*args, **kw):
        attempts[0] += 1
        if attempts[0] == 2:
            raise RuntimeError('Temporary Drive outage')
        return upload(*args, **kw)
    monkeypatch.setattr(native.drive, 'upload', flaky)
    h.add('book', note='Fix it')
    h.run()
    h.clock[0] += 10800
    h.run()
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'delivery'
    h.run()
    assert len(h.calls) == 1 and len(h.uploads) == 4
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'delivered'


def test_missing_corrections_spreadsheet_blocks_delivery(harness, monkeypatch):
    h = harness

    def workflow_without_audit(source, attachments, text, out, rules):
        out.mkdir(parents=True, exist_ok=True)
        for name, value in [('book.indd', b'corrected'), ('book.pdf', b'pdf'),
                            ('report.json', b'{}')]:
            (out / name).write_bytes(value)
        return {'status': 'verified', 'output_indd': str(out / 'book.indd'),
                'output_pdf': str(out / 'book.pdf'), 'report': str(out / 'report.json')}

    monkeypatch.setattr(native, '_call_workflow', workflow_without_audit)
    h.add('book', note='Fix it')
    h.run()
    h.clock[0] += 10800
    h.run()

    job = native._read_jobs(h.tmp_path)[0]
    assert job['status'] == 'technical_block'
    assert 'corrections.xlsx' in job['reason']
    assert not h.uploads
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'held'


def test_source_mismatch_before_workflow_keeps_all_slots_and_writes_audit(harness):
    h = harness
    h.files[0] = DriveFile('source', 'Sibley - Book 3.indd', 'application/octet-stream')
    h.add('book', [f'https://files.test/{i}.txt' for i in range(2)], note='Fix both')
    h.run()
    h.clock[0] += 10800
    h.run()

    assert not h.calls and not h.uploads
    state = queue.status(h.tmp_path)
    assert state['batches'][0]['state'] == 'held'
    job = native._read_jobs(h.tmp_path)[0]
    assert job['status'] == 'technical_block'
    assert job['expected_attachment_count'] == 2
    assert len(job['submission_receipts']) == 1
    assert len(job['submission_receipts'][0]['urls']) == 2
    audit = Path(job['result']['audit_spreadsheet'])
    assert audit.is_file() and audit.parent == Path(job['result']['job_dir']) / 'output'


def test_bad_remote_checksum_does_not_advance_registered_book(harness, monkeypatch):
    h = harness
    original = native.drive._json_call
    monkeypatch.setattr(native.drive, '_json_call', lambda *a, **kw: {**original(*a, **kw), 'md5Checksum': 'wrong'})
    h.add('book', note='Fix it')
    h.run()
    h.clock[0] += 10800
    h.run()
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'held'
    assert queue.books(h.tmp_path)['project']['source_version'] == 4


def test_conflicting_submission_during_native_work_blocks_upload(harness, monkeypatch):
    h = harness
    original = native._call_workflow
    def workflow(*args, **kw):
        result = original(*args, **kw)
        h.events[0]['values'][3]['value'] = 'Contradictory correction'
        intake.collect(h.tmp_path, h.ws, 'hs', opener=lambda *_: None)
        return result
    monkeypatch.setattr(native, '_call_workflow', workflow)
    h.add('first', note='Fix first')
    h.run()
    h.clock[0] += 10800
    h.run()
    assert len(h.calls) == 1 and not h.uploads
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'held'


def test_later_event_during_native_work_is_frozen_into_next_batch(harness, monkeypatch):
    h = harness
    original = native._call_workflow
    def workflow(*args, **kw):
        result = original(*args, **kw)
        if len(h.calls) == 1:
            h.add('second', note='Fix second')
            intake.collect(h.tmp_path, h.ws, 'hs', opener=lambda *_: None)
        return result
    monkeypatch.setattr(native, '_call_workflow', workflow)
    h.add('first', note='Fix first')
    h.run()
    h.clock[0] += 10800
    h.run()
    assert 'Fix second' not in h.calls[0]['text']
    assert len(queue.status(h.tmp_path)['batches']) == 1
    h.clock[0] += 10800
    h.run()
    assert len(h.calls) == 2 and 'Fix first' not in h.calls[1]['text']
    assert queue.books(h.tmp_path)['project']['source_version'] == 5.5


def test_project_read_failure_preserves_new_submissions(harness, monkeypatch):
    h = harness
    h.add('new', note='Fix it')
    monkeypatch.setattr(native, '_project_records', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('CRM unavailable')))
    report = h.run()
    state = queue.status(h.tmp_path)
    assert len(state['events']) == 1 and state['error'] and report.failed
    assert not h.calls


def test_tampered_frozen_attachment_blocks_delivery_retry(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(native.drive, 'upload', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('Offline')))
    h.add('first', ['https://files.test/1.txt'])
    h.run()
    h.clock[0] += 10800
    h.run()
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'delivery'
    h.calls[0]['attachments'][0].write_text('changed')
    h.run()
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'held' and len(h.calls) == 1


def test_invalid_word_attachment_stops_before_model_or_indesign(harness, monkeypatch):
    h = harness
    def broken(_token, _url, folder, **kw):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / 'corrections.docx'
        target.write_bytes(b'not a Word document')
        return target
    monkeypatch.setattr(native.native_files, 'download_file', broken)
    h.add('first', ['https://files.test/1.docx'])
    h.run()
    h.clock[0] += 10800
    h.run()
    assert not h.calls and not h.uploads
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'held'


def test_local_only_completion_blocks_next_batch_of_same_book(harness):
    h = harness
    h.ws.corrections_native_auto_upload = False
    h.add('first', note='Fix first')
    h.run()
    h.clock[0] += 10800
    h.run()
    h.add('second', note='Fix second')
    h.run()
    h.clock[0] += 10800
    h.run()
    assert len(h.calls) == 1 and not h.uploads
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'local_complete'


def test_explicit_local_release_delivers_saved_result_without_reapplying(harness):
    h = harness
    h.ws.corrections_native_auto_upload = False
    h.add('first', note='Fix first')
    h.run()
    h.clock[0] += 10800
    h.run()
    batch = queue.status(h.tmp_path)['batches'][0]
    queue.resume_delivery(h.tmp_path, batch['batch_id'])
    h.ws.corrections_native_auto_upload = True
    h.run()
    assert len(h.calls) == 1 and len(h.uploads) == 4
    assert queue.status(h.tmp_path)['batches'][0]['state'] == 'delivered'
    with pytest.raises(queue.QueueError):
        queue.resume_delivery(h.tmp_path, batch['batch_id'])


def test_held_batch_cannot_use_local_delivery_release(harness):
    h = harness
    h.add('first', note='Fix first')
    h.run()
    h.clock[0] += 10800
    intake.collect(h.tmp_path, h.ws, 'hs', opener=lambda *_: None)
    batch = queue.claim(h.tmp_path)
    queue.set_batch(h.tmp_path, batch['batch_id'], 'held')
    with pytest.raises(queue.QueueError, match='Held edits'):
        queue.resume_delivery(h.tmp_path, batch['batch_id'])


def test_corrupt_native_history_fails_closed(tmp_path):
    path = tmp_path / 'native_jobs' / 'old' / 'job.json'
    path.parent.mkdir(parents=True)
    path.write_text('{broken')
    with pytest.raises(native.NativeCorrectionError, match='unreadable'):
        native._read_jobs(tmp_path)


def test_front_matter_must_confirm_title_and_author():
    book = {'title': 'The Book', 'author': 'Bill Sibley'}
    verify_identity({'page_texts': [{'text': 'THE BOOK\nBill Sibley'}]}, book)
    with pytest.raises(ValueError, match='author'):
        verify_identity({'page_texts': [{'text': 'The Book\nSomebody Else'}]}, book)
    with pytest.raises(ValueError, match='title'):
        verify_identity({'page_texts': [{'text': 'Another Book\nBill Sibley'}]}, book)
