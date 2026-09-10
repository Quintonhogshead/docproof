"""Guarded form intake and delivery orchestration for the native desktop worker."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time

from app.lock import FolderLock
from . import drive, hubspot, native_queue as queue


def normalize(row, ws, registry, records):
    from .native_corrections import _row_marker, _timestamp_value, _project_name_properties
    fields = row.get('values') or row.get('fields') or []
    if isinstance(fields, dict):
        fields = [{'name': k, 'value': v} for k, v in fields.items()]
    mapped, duplicates = {}, set()
    for value in fields:
        if not isinstance(value, dict):
            continue
        name = str(value.get('name', '')).casefold()
        if name in mapped:
            duplicates.add(name)
        mapped[name] = str(value.get('value', '') or '')
    urls = hubspot.file_urls(mapped.get(ws.corrections_native_form_file_property.casefold(), ''))
    note = mapped.get(ws.corrections_native_form_notes_property.casefold(), '').strip()
    explicit = mapped.get(ws.corrections_native_form_project_property.casefold(), '').strip()
    associated = str(row.get('recordId') or row.get('objectId') or '').strip()
    pid = explicit or associated or None
    event = {'event_id': str(row.get('conversionId') or row.get('id') or hashlib.sha256(queue.canonical(row).encode()).hexdigest()),
             'marker': _row_marker(row), 'submitted_at': _timestamp_value(row.get('submittedAt')),
             'urls': urls, 'text': note, 'project_id': pid, 'reason': '',
             'raw': {'submittedAt': row.get('submittedAt'), 'conversionId': row.get('conversionId') or row.get('id'),
                     'values': fields, 'recordId': associated}}
    def hold(reason):
        event['reason'] = reason
        return event
    if duplicates:
        return hold('The form contains repeated field names; review the complete submission.')
    if not (row.get('conversionId') or row.get('id')) or event['submitted_at'] <= 0:
        return hold('The submission has no stable event ID or valid timestamp.')
    if explicit and associated and explicit != associated:
        return hold('The submitted Project ID contradicts its HubSpot association.')
    if not pid:
        return hold('A verified Project ID is required; submitter names are not used to guess a book.')
    if pid not in registry or pid not in records:
        return hold('This Project needs a verified book mapping and a readable HubSpot Project.')
    book, record = registry[pid], records[pid]
    titles = {queue.title_key(value) for value in [book['title'], *book['title_aliases']]}
    submitted_title = mapped.get(ws.corrections_native_form_book_property.casefold(), '')
    crm_title = record.properties.get(ws.corrections_native_project_book_property, '')
    if queue.title_key(submitted_title) not in titles or queue.title_key(crm_title) not in titles:
        return hold('The form title, Project title and registered book do not agree.')
    first, last = _project_name_properties(ws)
    crm_author = ' '.join(str(record.properties.get(k, '')).strip() for k in (first, last)).strip()
    authors = {queue.title_key(value) for value in [book['author'], *book['author_aliases']]}
    if queue.title_key(crm_author) not in authors:
        return hold('The Project author does not agree with the registered book.')
    count_field = ws.corrections_native_form_file_count_property
    if count_field:
        expected = mapped.get(count_field.casefold(), '')
        if not expected.isdecimal() or int(expected) != len(urls):
            return hold('The declared attachment count does not match the files HubSpot received.')
    if not urls and not note:
        return hold('The submission contains no received correction files or notes.')
    return event


def collect(home, ws, hs_token, *, opener, now=None):
    """Capture every event, independently of the InDesign worker lock."""
    from . import native_corrections as native
    now = time.time() if now is None else now
    if type(ws.corrections_native_quiet_seconds) is not int or ws.corrections_native_quiet_seconds < queue.QUIET_SECONDS:
        raise queue.QueueError('The native quiet period must be at least three hours.')
    if not ws.corrections_native_form_book_property or not ws.corrections_native_project_book_property:
        raise queue.QueueError('Configure both form and Project book-title fields before collection.')
    try:
        rows = native.form_submissions(hs_token, ws.corrections_native_form_id, opener=opener)
        cutoff = native._timestamp_value(ws.corrections_native_start_after)
        if cutoff <= 0:
            raise queue.QueueError('A valid submission starting date is required.')
        rows = [row for row in rows if native._timestamp_value(row.get('submittedAt')) > cutoff
                or native._timestamp_value(row.get('submittedAt')) <= 0]
        registry = queue.books(home)
        queue.import_history(home, native._read_jobs(home))
        try:
            records = {record.id: record for record in native._project_records(hs_token, ws, opener=opener)}
        except Exception:
            # Preserve newly received raw events even when CRM enrichment is
            # unavailable. Existing frozen batches are left intact; the error
            # below prevents new work until a complete read succeeds.
            existing = {event['marker'] for event in queue.status(home)['events']}
            queue.capture(home, ws.corrections_native_form_id,
                          [normalize(row, ws, registry, {}) for row in rows if native._row_marker(row) not in existing], now=now)
            raise
        queue.capture(home, ws.corrections_native_form_id,
                      [normalize(row, ws, registry, records) for row in rows], now=now)
    except Exception as exc:
        queue.record_error(home, f'{type(exc).__name__}: intake failed; no new batch will start.')
        raise
    return queue.status(home, now=now, quiet_seconds=ws.corrections_native_quiet_seconds)


def _event(batch):
    urls = [url for event in batch['events'] for url in event['urls']]
    notes = '\n\n'.join(f"Submission {event['marker']}:\n{event['text']}"
                        for event in batch['events'] if event['text'])
    return urls, notes, 'batch:' + batch['batch_id']


def _job(home, batch):
    from .native_corrections import _record_identity
    urls, notes, marker = _event(batch)
    identity = _record_identity(hubspot.HubSpotRecord(batch['project_id'], {}), marker, urls, notes)
    jid = hashlib.sha256(identity.encode()).hexdigest()[:24]
    path = Path(home) / 'native_jobs' / jid / 'job.json'
    return jid, json.loads(path.read_text('utf-8')) if path.exists() else None


def _prework_receipt(home, batch, reason):
    """Create an auditable held receipt when guarded discovery fails early."""
    from . import native_corrections as native

    jid, saved = _job(home, batch)
    job_dir = Path(home) / 'native_jobs' / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    job = saved if isinstance(saved, dict) else None
    if job is None:
        events = batch.get('events') or []
        slots, receipts, notes = [], [], []
        for event in events:
            urls = event.get('urls') or []
            placeholders = []
            for _ in urls:
                placeholders.append(f'attachment-slot-{len(slots) + 1}')
                slots.append(placeholders[-1])
            receipts.append({'marker': str(event.get('marker') or ''), 'urls': placeholders})
            if event.get('text'):
                notes.append(str(event['text']))
        book = batch.get('book') or {}
        job = {
            'job_id': jid, 'identity': jid, 'record_id': str(batch.get('project_id') or ''),
            'source_id': str(book.get('source_id') or ''), 'source_name': '',
            'source_version': book.get('source_version'), 'folder_id': str(book.get('folder_id') or ''),
            'submission_marker': 'batch:' + str(batch.get('batch_id') or ''),
            'submission_urls': slots, 'expected_attachment_count': len(slots),
            'submission_text': '\n\n'.join(notes), 'record_properties': {},
            'submission_receipts': receipts, 'batch_id': batch.get('batch_id'),
            'book_identity': book, 'status': 'technical_block', 'blocked': True,
            'uploaded': {}, 'created_at': time.time(), 'reason': str(reason),
        }
    else:
        job['status'] = 'technical_block'
        job['blocked'] = True
        job['reason'] = str(reason)
    native._write_minimal_audit(job_dir, job, status='technical_block', reason=str(reason))
    native._write_json(job_dir / 'job.json', job)
    return jid, job


def _work(token, batch, ws, home, opener):
    from . import native_corrections as native
    book = batch['book']
    listing = drive.list_folder(token, book['folder_id'], opener=opener)
    jid, job = _job(home, batch)
    source = next((row for row in listing if row.id == book['source_id']), None)
    parsed = native.versioned_name(source.name) if source else None
    if not parsed or (parsed[0].casefold(), parsed[1]) != (book['surname'].casefold(), book['source_version']):
        raise queue.QueueError('The registered source is missing or its author/version changed.')
    next_book = native.next_version(book['source_version'])
    own_names = {native.native_filename(book['surname'], next_book, suffix)
                 for suffix in ('.indd', '.pdf', '.report.json', ' - package.zip')}
    owned = [row for row in listing if row.name in own_names and row.app_properties.get(native.NATIVE_JOB_PROP) == jid]
    candidates = [row for row in listing if row not in owned]
    highest, issue = native.pick_source(candidates, book['surname'])
    if issue or not highest or highest.id != source.id:
        raise queue.QueueError('The highest book version is ambiguous or is not the registered source. Confirm the new source before processing.')
    props = {'_docproof_folder': book['folder_id'], '_docproof_first': book['author'], '_docproof_last': book['surname']}
    record = hubspot.HubSpotRecord(book['project_id'], props)
    return (record, book['folder_id'], native.NativeSource(source, book['source_version'], book['surname']),
            listing, _event(batch), batch)


def verify_uploads(token, home, batch, job, *, opener):
    """Read back Drive identities and content checksums before delivery is complete."""
    from . import native_corrections as native
    folder = batch['book']['folder_id']
    artifacts = native._artifact_paths(job['result'], Path('.'), Path(home) / 'native_jobs' / job['job_id'] / 'output',
                                      native.native_filename(batch['book']['surname'], native.next_version(batch['book']['source_version'])))
    if not artifacts or set(job.get('artifact_hashes') or {}) != {name for _, name in artifacts}:
        raise queue.QueueError('The delivery artifact manifest is incomplete.')
    for path, name in artifacts:
        fid = job.get('uploaded', {}).get(name)
        if not fid:
            return False
        metadata = drive._json_call(drive._request(drive._url(f'{drive.API}/files/{fid}', {
            'fields': 'id,name,parents,size,md5Checksum,sha256Checksum,appProperties,trashed', **drive.SHARED_DRIVE}), token),
            opener=opener, what='verify the uploaded native correction file')
        props = metadata.get('appProperties') or {}
        if (metadata.get('id') != fid or metadata.get('name') != name or folder not in metadata.get('parents', [])
                or metadata.get('trashed') or int(metadata.get('size', -1)) != path.stat().st_size
                or props.get(native.NATIVE_JOB_PROP) != job['job_id']
                or props.get('docproof.native_hash') != native._hash(path)):
            raise queue.QueueError('The uploaded file identity, destination, size or receipt does not match the verified local result.')
        sha = metadata.get('sha256Checksum')
        if sha:
            good = sha == native._hash(path)
        else:
            with path.open('rb') as stream:
                actual_md5 = hashlib.file_digest(stream, 'md5').hexdigest()
            good = bool(metadata.get('md5Checksum')) and metadata['md5Checksum'] == actual_md5
        if not good:
            raise queue.QueueError('Drive content verification failed; the book has not advanced.')
    return True


def run_stage(token, home, ws, *, opener, hs_token, report, mock=False):
    from . import native_corrections as native
    # Discovery runs even while another worker owns InDesign. Claims and the
    # native operation are serialized by the existing per-home worker lock.
    collect(home, ws, hs_token, opener=opener)
    guarded = replace(ws, corrections_native_partial_upload=False, hubspot_write_back=False,
                      corrections_native_folder_property='_docproof_folder',
                      corrections_native_project_first_property='_docproof_first',
                      corrections_native_project_last_property='_docproof_last')
    with FolderLock(Path(home) / 'native_jobs'):
        batch = None
        for candidate in queue.pending_batches(home):
            _, saved = _job(home, candidate)
            if candidate['state'] == 'awaiting_attachment' and saved and not native._manual_files_ready(saved, home):
                continue
            batch = candidate
            break
        if batch is None:
            batch = queue.claim(home, quiet_seconds=ws.corrections_native_quiet_seconds)
        if batch is None:
            state = queue.status(home, quiet_seconds=ws.corrections_native_quiet_seconds)
            report.waiting += sum(not event['batch_id'] and not event['history_state'] for event in state['events'])
            report.needs_human.extend((event['marker'], event['reason']) for event in state['events'] if event['reason'])
            report.needs_human.extend((b['project_id'], b['reason']) for b in state['batches'] if b['state'] == 'held')
            return
        try:
            queue.assert_active(home, batch['batch_id'])
            work = _work(token, batch, guarded, home, opener)
            if mock:
                return
            queue.set_batch(home, batch['batch_id'], 'running')
            native._run_one(token, home, guarded, work, mock=False, opener=opener, hs_token=hs_token, report=report)
            jid, job = _job(home, batch)
            if not job:
                raise queue.QueueError('The native batch did not create a durable job receipt.')
            if job.get('blocked') or job.get('held') or job.get('status') in {'technical_block', 'designer_needed', 'clarification_needed'}:
                queue.set_batch(home, batch['batch_id'], 'held', reason=str(job.get('reason') or job.get('held') or job.get('status')), job_id=jid)
            elif job.get('status') == 'awaiting_attachment':
                queue.set_batch(home, batch['batch_id'], 'awaiting_attachment', reason='Waiting for all submitted files.', job_id=jid)
            elif job.get('local_complete') and not ws.corrections_native_auto_upload:
                queue.set_batch(home, batch['batch_id'], 'local_complete', reason='Verified locally; uploads are disabled.', job_id=jid)
            elif job.get('status') == 'verified':
                queue.assert_active(home, batch['batch_id'])
                if job.get('delivery_error'):
                    queue.set_batch(home, batch['batch_id'], 'delivery', reason='Delivery will resume from the saved result.', job_id=jid)
                elif verify_uploads(token, home, batch, job, opener=opener):
                    if ws.hubspot_write_back and not job.get('crm_written'):
                        props = native._status_props(ws, job['result'], output_names=list(job['artifact_hashes']))
                        if props:
                            hubspot.set_properties(hs_token, ws.hubspot_object, batch['project_id'], props,
                                                   allow=set(props), opener=opener)
                            job['crm_written'] = True
                            native._write_json(Path(home) / 'native_jobs' / jid / 'job.json', job)
                    next_book = native.next_version(batch['book']['source_version'])
                    name = native.native_filename(batch['book']['surname'], next_book)
                    queue.advance_book(home, batch['batch_id'], job['uploaded'][name], next_book)
                    queue.set_batch(home, batch['batch_id'], 'delivered', job_id=jid)
                else:
                    queue.set_batch(home, batch['batch_id'], 'delivery', reason='Upload will resume from the saved result.', job_id=jid)
            else:
                raise queue.QueueError('The batch stopped without a verified or held outcome.')
        except queue.QueueError as exc:
            jid, _ = _prework_receipt(home, batch, str(exc))
            queue.set_batch(home, batch['batch_id'], 'held', reason=str(exc), job_id=jid)
            report.needs_human.append((batch['project_id'], str(exc)))
        except Exception:
            # A process/connection failure cannot reset a claimed batch or
            # release its book. The existing native receipt controls recovery.
            report.failed.append((batch['project_id'], 'The batch is preserved for recovery; its book remains reserved.'))
            raise
