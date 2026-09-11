"""The CRM-free formatting contract, using real Word ZIPs and a fake Drive."""
import io
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document
from docx.shared import Pt

from app.watch import formatting
from app.watch.drive import DriveFile, FOLDER_MIME
from app.watch.settings import WatchSettings
from app.watch.stages import OUTPUT_PROP, SOURCE_PROP, STATE_PROP, is_proof_candidate
from app.watch.state import FileRecord, WatchState
from app.watch.tick import tick
from .fakes import drive_entry, fake_drive, http_error, TaggingProvider
from .conftest import FIXTURES


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    provider = TaggingProvider()
    monkeypatch.setattr("app.jobs.build_provider", lambda cfg, api_key=None: provider)
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    return provider


def manuscript():
    return (FIXTURES / "googledoc.docx").read_bytes()


def entry(name, parent='rootfolder', **kw):
    return {**drive_entry(name, **kw), 'parents': [parent]}


def settings(**kw):
    return WatchSettings(folder_id='rootfolder', client_id='client', client_secret='secret', **kw)


def run(tmp_path, files=None, ws=None, opener=None, **kw):
    opener = opener or fake_drive(files, docx=manuscript(), page_size=1)
    keys = []
    def key(name):
        keys.append(name)
        assert name == 'google', 'Formatting must not even read a HubSpot credential'
        return 'refresh'
    report = tick(tmp_path, ws or settings(), opener=opener, get_key=key, **kw)
    return report, opener


def test_identical_surnames_nested_folders_no_hubspot(tmp_path):
    files = {'parent': entry('Authors', mime=FOLDER_MIME),
             'a': entry('Jane Smith', 'parent', mime=FOLDER_MIME),
             'b': entry('Jim Smith', 'parent', mime=FOLDER_MIME),
             'one': entry('Smith - Book Original.docx', 'a'),
             'two': entry('Smith - Book Original.docx', 'b')}
    report, opener = run(tmp_path, files, settings(hubspot_enabled=True, subfolders_enabled=True))
    assert report.ok, report.failed
    outputs = [e for e in opener.files.values() if e.get('appProperties', {}).get(OUTPUT_PROP)]
    assert len(outputs) == 2
    assert {tuple(e['parents']) for e in outputs} == {('a',), ('b',)}
    assert {e['name'] for e in outputs} == {'Smith - Book One.docx'}
    for source in ('one', 'two'):
        assert opener.files[source]['name'] == 'Smith - Book Original_done.docx'
    again, _ = run(tmp_path, opener=opener)
    assert again.ok and again.new == 0 and not again.uploaded
    assert not any('hubapi' in req.full_url for req in opener.calls)
    assert is_proof_candidate(DriveFile.from_api(outputs[0]))


@pytest.mark.parametrize('name', ['Book Original.docx', 'Typo [Book Original].DOCX',
                                  'Smith — BOOK ORIGINAL.docx', 'Smith Book Original'])
def test_label_does_not_require_a_matching_name(tmp_path, name):
    report, opener = run(tmp_path, {'src': entry(name)})
    assert report.ok and len(report.uploaded) == 1
    assert '_done' in opener.files['src']['name']


@pytest.mark.parametrize('evidence', ['marker', 'state', 'book 0', 'Book 1', 'Book One', 'Book Two', 'orphan'])
def test_migration_never_reformats_already_delivered_books(tmp_path, evidence):
    files = {'src': entry('Typo - Book Original.docx')}
    if evidence == 'marker':
        files['src']['appProperties'] = {STATE_PROP: 'formatted'}
    elif evidence == 'state':
        WatchState(tmp_path / 'state.json').record(FileRecord(file_id='src', marked='formatted'))
    else:
        files['old'] = entry('Correct spelling - ' + ('Book One' if evidence == 'orphan' else evidence) + '.docx')
        if evidence == 'orphan':
            files['old']['appProperties'] = {OUTPUT_PROP: 'format', SOURCE_PROP: 'src'}
    report, opener = run(tmp_path, files)
    assert report.ok and not report.uploaded and not report.prepped
    assert opener.files['src']['name'].endswith('Original_done.docx')
    assert not any('alt=media' in req.full_url for req in opener.calls)


def test_done_failed_and_unrelated_files_are_not_formatted(tmp_path):
    files = {'done': entry('Smith - Book Original_done.docx'),
             'failed': entry('Smith - Book Original.docx', props={STATE_PROP: 'failed'}),
             'notes': entry('Questionnaire.docx')}
    report, opener = run(tmp_path, files)
    assert not report.uploaded and len(report.needs_human) == 1
    assert not any(req.get_method() == 'PATCH' for req in opener.calls)


def test_dry_run_is_read_only_and_excludes_old_books(tmp_path):
    files = {'src': entry('Smith - Book Original.docx'), 'old': entry('Smith - Book One.docx')}
    report, opener = run(tmp_path, files, dry_run=True)
    assert report.new == 0 and not report.plan
    assert not any(tmp_path.iterdir())
    assert not any(req.get_method() == 'PATCH' for req in opener.calls)


def test_ambiguous_originals_require_review(tmp_path):
    report, opener = run(tmp_path, {'one': entry('Book Original.docx'),
                                   'two': entry('Book Original copy.docx')})
    assert len(report.needs_human) == 2 and not report.uploaded
    assert not any(req.get_method() == 'PATCH' for req in opener.calls)


@pytest.mark.parametrize('failure', ['upload', 'patch'])
def test_delivery_failure_retry_does_not_duplicate_or_rename_early(tmp_path, failure, provider):
    opener = fake_drive({'src': entry('Smith - Book Original.docx')}, docx=manuscript(),
                        fail={failure: http_error(500)})
    report, _ = run(tmp_path, opener=opener)
    assert report.failed
    assert opener.files['src']['name'] == 'Smith - Book Original.docx'
    calls_before_retry = len(provider.calls)
    report, _ = run(tmp_path, opener=opener)
    assert report.ok
    assert len(provider.calls) == calls_before_retry
    assert opener.files['src']['name'] == 'Smith - Book Original_done.docx'
    outputs = [e for e in opener.files.values() if e.get('appProperties', {}).get(OUTPUT_PROP)]
    assert len(outputs) == 1


def test_existing_exporter_keeps_12_point_body_and_14_point_chapters(tmp_path):
    report, opener = run(tmp_path, {'src': entry('Smith - Book Original.docx')})
    assert report.ok
    output = next(e for e in opener.files.values() if e.get('appProperties', {}).get(OUTPUT_PROP))
    doc = Document(io.BytesIO(opener.content[output['id']]))
    assert doc.styles['body para'].font.name == 'Times New Roman'
    assert doc.styles['body para'].font.size == Pt(12)
    assert doc.styles['chapter # / title'].font.name == 'Times New Roman'
    assert doc.styles['chapter # / title'].font.size == Pt(14)


def test_other_stage_failure_does_not_block_formatting(tmp_path):
    report, opener = run(tmp_path, {'src': entry('Book Original.docx')}, settings(proofing_enabled=True))
    assert report.uploaded == ['Book One.docx']
    assert report.failed[0][0] == 'Other automations'


def test_cap_defers_new_books_but_migrates_old_ones(tmp_path):
    files = {}
    for i in range(3):
        files[f'f{i}'] = entry(f'Author {i}', mime=FOLDER_MIME)
        files[f's{i}'] = entry('Book Original.docx', f'f{i}')
    files['old'] = entry('Book One.docx', 'f0')
    report, opener = run(tmp_path, files, settings(max_files_per_tick=1))
    assert report.ok and report.deferred == 1 and len(report.uploaded) == 1
    assert opener.files['s0']['name'] == 'Book Original_done.docx'


def test_new_book_one_can_enter_existing_proofing_workflow(tmp_path):
    from .test_watch_proof import sub_proof_ws, ready_to_proof
    ws = sub_proof_ws(formatting_drive_only=True, folder_id='rootfolder', proof_runner='external')
    files = {'author': entry('Quinton Johnson', mime=FOLDER_MIME),
             'src': entry('Johnson - Book Original.docx', 'author')}
    opener = fake_drive(files, docx=manuscript(), hubspot={'Johnson': ready_to_proof()})
    report = tick(tmp_path, ws, opener=opener, get_key=lambda name: 'test-key')
    assert report.ok, report.failed
    assert report.uploaded == ['Johnson - Book One.docx']
    assert [name for name, _ in report.awaiting_proof] == ['Johnson - Book One.docx']
    assert not any('hubapi' in req.full_url and req.get_method() == 'PATCH' for req in opener.calls)


def test_preserves_native_google_doc_support(tmp_path):
    from app.watch.drive import GOOGLE_DOC_MIME
    report, opener = run(tmp_path, {'src': entry('Smith - Book Original.docx', mime=GOOGLE_DOC_MIME)})
    assert report.ok and report.uploaded == ['Smith - Book One.docx']
    assert opener.files['src']['name'] == 'Smith - Book Original_done.docx'


def test_old_internal_exports_and_configured_archive_are_never_intake(tmp_path):
    files = {'old': entry('book_Smith - Book Original.docx'),
             'archive': entry('Archive', mime=FOLDER_MIME),
             'backup': entry('source - Smith - Book Original.docx', 'archive')}
    report, opener = run(tmp_path, files, settings(archive_folder_id='archive'))
    assert report.ok and not report.uploaded and not report.new
