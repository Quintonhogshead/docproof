from types import SimpleNamespace
import json

import pytest

from app.watch import native_corrections as native
from app.watch import native_intake
from app.watch import native_queue as queue
from app.watch import hubspot
from app.watch.drive import DriveFile


def ws(**overrides):
    base = dict(corrections_native_shared_form=True,
                corrections_native_form_project_property='project',
                corrections_native_form_book_property='book',
                corrections_native_form_first_property='first',
                corrections_native_form_last_property='last',
                corrections_native_project_first_property='first',
                corrections_native_project_last_property='last',
                corrections_native_project_book_property='title',
                corrections_native_form_file_property='files',
                corrections_native_form_notes_property='notes',
                corrections_native_form_file_count_property='',
                corrections_native_form_id='form',
                corrections_native_start_after='',
                corrections_folder_name='Interior Design',
                corrections_native_folder_property='',
                corrections_native_project_id_property='project_id')
    base.update(overrides)
    return SimpleNamespace(**base)


def record(pid, title='Quiet River', first='Bill', last='Sibley'):
    return hubspot.HubSpotRecord(pid, {'title': title, 'first': first, 'last': last})


def row(title='Quiet River', first='Nancy', last='Jones'):
    return {'id': 'event-1', 'submittedAt': '2026-01-01T00:00:00Z',
            'values': [{'name': 'book', 'value': title}, {'name': 'first', 'value': first},
                       {'name': 'last', 'value': last}, {'name': 'notes', 'value': 'Fix typo'}]}


def test_shared_form_ignores_proxy_submitter_and_matches_unique_title():
    registry = {'p1': {'title': 'Quiet River', 'author': 'Bill Sibley',
                       'surname': 'Sibley', 'title_aliases': [], 'author_aliases': []}}
    event = native_intake.normalize(row(), ws(), registry, {'p1': record('p1')})
    assert event['project_id'] == 'p1' and not event['reason']


def test_automatic_registration_cannot_replace_a_mapping_created_since_listing(tmp_path):
    first = dict(project_id='p1', title='Quiet River', author='Bill Sibley', surname='Sibley',
                 folder_id='interior', source_id='source4', source_version=4)
    saved = queue.register_book(tmp_path, first)
    raced = queue.register_book(tmp_path, {**first, 'source_id':'source5', 'source_version':5}, if_absent=True)
    assert raced == saved
    assert queue.books(tmp_path)['p1']['source_id'] == 'source4'


@pytest.mark.parametrize('records,reason', [
    ({'a': record('a'), 'b': record('b')}, 'exactly one'),
    ({'a': record('a', first='', last='')}, 'author'),
])
def test_shared_form_holds_duplicate_title_or_missing_crm_author(records, reason):
    registry = {key: {'title': value.properties['title'], 'author': 'Bill Sibley',
                      'surname': 'Sibley', 'title_aliases': [], 'author_aliases': []}
                for key, value in records.items()}
    event = native_intake.normalize(row(), ws(), registry, records)
    if reason == 'author':
        assert event['project_id'] == 'a'
    else:
        assert event['project_id'] is None
    assert reason in event['reason'].lower()


def test_auto_registration_selects_unique_highest_half_source(tmp_path, monkeypatch):
    rec = record('p1')
    folder = DriveFile('author', 'Bill Sibley', 'application/vnd.google-apps.folder')
    interior = DriveFile('interior', 'Interior Design', 'application/vnd.google-apps.folder')
    files = [DriveFile('old', 'Sibley - Book 4.indd', 'application/octet-stream'),
             DriveFile('new', 'Sibley - Book 4.5.indd', 'application/octet-stream')]
    calls = []
    def listing(token, folder_id, *, opener):
        calls.append(folder_id)
        return [folder] if folder_id == 'root' else [interior] if folder_id == 'author' else files
    monkeypatch.setattr(native_intake.drive, 'list_folder', listing)
    holds = native_intake._auto_register(tmp_path, ws(folder_id='root'), {'p1': rec},
                                         [row(first='Nancy', last='Sibley')], {}, 'drive', None)
    assert not holds and calls == ['root', 'author', 'interior']
    saved = queue.books(tmp_path)['p1']
    assert saved['source_id'] == 'new' and saved['source_version'] == 4.5


def test_auto_registration_holds_duplicate_author_and_does_not_rewrite_registry(tmp_path, monkeypatch):
    records = {'a': record('a'), 'b': record('b', title='Other', first='Bill', last='Sibley')}
    monkeypatch.setattr(native_intake.drive, 'list_folder', lambda *a, **k: [])
    holds = native_intake._auto_register(tmp_path, ws(folder_id='root'), records, [row()], {}, 'drive', None)
    assert 'author' in next(iter(holds.values())).lower()
    mapped = {'a': {'project_id': 'a', 'title': 'Quiet River'}}
    def no_drive(*args, **kwargs):
        raise AssertionError('mapped books must not trigger Drive onboarding')
    monkeypatch.setattr(native_intake.drive, 'list_folder', no_drive)
    assert native_intake._auto_register(tmp_path, ws(folder_id='root'), {'a': record('a')}, [row()], mapped, 'drive', None) == {}


@pytest.mark.parametrize('root_children,author_children,interior_children', [
    ([DriveFile('a', 'Bill Sibley', 'application/vnd.google-apps.folder'),
      DriveFile('b', 'bill sibley', 'application/vnd.google-apps.folder')], [], []),
    ([DriveFile('a', 'Bill Sibley', 'application/vnd.google-apps.folder')],
     [DriveFile('i', 'Interior Design', 'application/vnd.google-apps.folder'),
      DriveFile('j', 'interior design', 'application/vnd.google-apps.folder')], []),
    ([DriveFile('a', 'Bill Sibley', 'application/vnd.google-apps.folder')],
     [DriveFile('i', 'Interior Design', 'application/vnd.google-apps.folder')],
     [DriveFile('x', 'Sibley - Book 4.5.indd', 'application/octet-stream'),
      DriveFile('y', 'Sibley - Book 4.5.indd', 'application/octet-stream')]),
])
def test_auto_registration_holds_duplicate_folders_or_highest(tmp_path, monkeypatch,
                                                               root_children, author_children, interior_children):
    def listing(token, folder_id, *, opener):
        return root_children if folder_id == 'root' else author_children if folder_id == 'a' else interior_children
    monkeypatch.setattr(native_intake.drive, 'list_folder', listing)
    holds = native_intake._auto_register(tmp_path, ws(folder_id='root'), {'p1': record('p1')},
                                         [row(last='Sibley')], {}, 'drive', None)
    assert holds and 'multiple' in next(iter(holds.values())).lower() or 'unique' in next(iter(holds.values())).lower()


def test_shared_form_missing_title_is_held():
    event = native_intake.normalize(row(title=''), ws(), {}, {'p1': record('p1')})
    assert event['project_id'] is None and 'title' in event['reason'].lower()


def test_shared_form_does_not_fuzzy_match_title():
    event = native_intake.normalize(row(title='Quiet Rvr'), ws(), {}, {'p1': record('p1')})
    assert event['project_id'] is None and 'exactly one' in event['reason'].lower()


def test_project_records_pagination_cycle_is_refused(monkeypatch):
    class Request:
        def __init__(self, url): self.full_url = url
    monkeypatch.setattr(hubspot, '_request', lambda url, token, **kw: Request(url))
    monkeypatch.setattr(hubspot, '_json_call', lambda request, **kw: {
        'results': [{'id': '1', 'properties': {'title': 'Quiet River'}}],
        'paging': {'next': {'after': 'same'}}})
    with pytest.raises(Exception, match='pagination'):
        native._project_records('token', ws(hubspot_object='projects'), opener=None)


def test_project_records_get_pagination_includes_missing_first(monkeypatch):
    calls = []
    monkeypatch.setattr(hubspot, '_request', lambda url, token, **kw: calls.append(url) or object())
    pages = iter([{'results': [{'id': '1', 'properties': {'title': 'One'}}],
                   'paging': {'next': {'after': 'next'}}},
                  {'results': [{'id': '2', 'properties': {'title': 'Two', 'last': ''}}]}])
    monkeypatch.setattr(hubspot, '_json_call', lambda *a, **k: next(pages))
    records = native._project_records('token', ws(hubspot_object='projects'), opener=None)
    assert [record.id for record in records] == ['1', '2']
    assert 'after=next' in calls[1]


def test_collect_drive_failure_captures_raw_event_with_bounded_reason(tmp_path, monkeypatch):
    from app.watch.settings import WatchSettings
    settings = WatchSettings(corrections_native_shared_form=True,
                             corrections_native_form_book_property='book',
                             corrections_native_project_book_property='title',
                             corrections_native_form_poll=True,
                             corrections_native_start_after='2020-01-01T00:00:00Z')
    submission = row()
    monkeypatch.setattr(native, 'form_submissions', lambda *a, **k: [submission])
    monkeypatch.setattr(native, '_project_records', lambda *a, **k: {'p1': record('p1')}.values())
    monkeypatch.setattr(native_intake.drive, 'list_folder', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('secret-token-leak')))
    native_intake.collect(tmp_path, settings, 'hubspot', opener=None, drive_token='drive')
    events = queue.status(tmp_path)['events']
    with queue.transaction(tmp_path) as db:
        raw = json.loads(db.execute('SELECT payload FROM events').fetchone()['payload'])
    assert events and raw['raw']['values'] and 'secret-token' not in events[0]['reason']


def test_legacy_project_search_remains_post(monkeypatch):
    # The legacy path remains covered by the existing watcher contract: this
    # test pins the request shape used when shared-form mode is disabled.
    calls = []
    monkeypatch.setattr(hubspot, '_request', lambda url, token, **kw: calls.append((url, kw)) or object())
    monkeypatch.setattr(hubspot, '_json_call', lambda *a, **k: {'results': []})
    native._project_records('token', ws(corrections_native_shared_form=False,
                                        hubspot_object='projects'), opener=None)
    assert calls and calls[0][1].get('method') == 'POST'
