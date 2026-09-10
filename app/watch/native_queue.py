"""Transactional native-correction inbox, verified book registry and batch ledger.

No network or model calls. Receipt capture and claims are short SQLite
transactions, so a collector can keep receiving submissions during InDesign.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata

QUIET_SECONDS = 3 * 60 * 60
DB_NAME = 'native-queue.sqlite3'


class QueueError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def title_key(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', str(value)).casefold()))


@contextmanager
def transaction(home: Path):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / DB_NAME, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS books (
                project_id TEXT PRIMARY KEY, folder_id TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL, blocked TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, form_id TEXT NOT NULL, marker TEXT NOT NULL,
                payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
                submitted_at REAL NOT NULL, received_at REAL NOT NULL,
                project_id TEXT, reason TEXT NOT NULL DEFAULT '', batch_id TEXT);
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, data TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                job_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS history (
                project_id TEXT NOT NULL, marker TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY(project_id, marker));
            CREATE TABLE IF NOT EXISTS conflicts (
                event_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(event_id, payload_hash));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def register_book(home, book):
    """An operator supplies the confirmed Project/folder/source mapping."""
    required = ('project_id', 'title', 'author', 'surname', 'folder_id', 'source_id')
    data = {key: str(book.get(key, '')).strip() for key in required}
    if any(not value or len(value) > 500 for value in data.values()):
        raise QueueError('A verified book needs its Project ID, title, author, surname, folder ID and source ID.')
    for key in ('project_id', 'folder_id', 'source_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', data[key]):
            raise QueueError(f'Invalid {key.replace("_", " ")}.')
    version = book.get('source_version')
    if type(version) is not int or version < 1:
        raise QueueError('The confirmed source needs a positive Book version.')
    data['source_version'] = version
    for key in ('title_aliases', 'author_aliases'):
        values = book.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values):
            raise QueueError('Book aliases must be a list of explicit names.')
        data[key] = list(dict.fromkeys(v.strip() for v in values))
    with transaction(home) as db:
        old = db.execute('SELECT data FROM books WHERE project_id=?', (data['project_id'],)).fetchone()
        if old and json.loads(old['data']) == data:
            return data
        active = db.execute("SELECT 1 FROM batches WHERE project_id=? AND state NOT IN ('delivered','released')",
                            (data['project_id'],)).fetchone()
        if active:
            raise QueueError('This book has an unfinished batch. Resolve it before changing its mapping.')
        try:
            db.execute('INSERT INTO books(project_id,folder_id,data) VALUES(?,?,?) '
                       'ON CONFLICT(project_id) DO UPDATE SET folder_id=excluded.folder_id,data=excluded.data',
                       (data['project_id'], data['folder_id'], canonical(data)))
        except sqlite3.IntegrityError as exc:
            raise QueueError('Each book must have its own Interior Design folder; this folder already belongs to another Project.') from exc
    return data


def books(home):
    with transaction(home) as db:
        return {row['project_id']: json.loads(row['data']) for row in db.execute('SELECT * FROM books')}


def import_history(home, jobs):
    """Retain exact old submission markers, including blocked unfinished work."""
    with transaction(home) as db:
        for job in jobs:
            pid, marker = str(job.get('record_id') or ''), str(job.get('submission_marker') or '')
            if not pid or not marker or marker.startswith('batch:'):
                continue
            done = (job.get('status') == 'verified' and not job.get('blocked') and not job.get('held')
                    and bool(job.get('uploaded')) and bool(job.get('artifact_hashes'))
                    and set(job.get('artifact_hashes') or {}).issubset(job.get('uploaded') or {}))
            state = 'delivered' if done else 'held'
            db.execute('INSERT INTO history VALUES(?,?,?) ON CONFLICT(project_id,marker) '
                       'DO UPDATE SET state=excluded.state', (pid, marker, state))


def capture(home, form_id, events, *, now=None):
    """Capture all normalized events before work selection, preserving conflicts.

    Normalized events include event_id, marker, submitted_at, urls, text,
    project_id (if known), reason and the original form payload.
    """
    now = time.time() if now is None else now
    with transaction(home) as db:
        for event in events:
            eid = hashlib.sha256((form_id + '|' + event['event_id']).encode()).hexdigest()
            body = canonical({k: event[k] for k in ('marker', 'submitted_at', 'urls', 'text', 'raw')})
            sha = hashlib.sha256(body.encode()).hexdigest()
            prior = db.execute('SELECT * FROM events WHERE event_id=?', (eid,)).fetchone()
            if prior:
                if prior['payload_hash'] != sha:
                    db.execute('INSERT OR IGNORE INTO conflicts VALUES(?,?,?)', (eid, sha, body))
                    reason = 'A previously captured submission changed; its original receipt was preserved.'
                    db.execute('UPDATE events SET reason=? WHERE event_id=?', (reason, eid))
                    if prior['project_id']:
                        db.execute('UPDATE books SET blocked=? WHERE project_id=?', (reason, prior['project_id']))
                    if prior['batch_id']:
                        db.execute("UPDATE batches SET state='held',reason=? WHERE batch_id=?", (reason, prior['batch_id']))
                    continue
                if prior['batch_id'] and (event.get('reason') or prior['project_id'] != event.get('project_id')):
                    reason = event.get('reason') or 'The Project identity changed after this batch was frozen.'
                    db.execute('UPDATE events SET reason=? WHERE event_id=?', (reason, eid))
                    db.execute("UPDATE batches SET state='held',reason=? WHERE batch_id=?", (reason, prior['batch_id']))
                    db.execute('UPDATE books SET blocked=? WHERE project_id=?', (reason, prior['project_id']))
                    continue
                if prior['batch_id'] is None and not db.execute('SELECT 1 FROM conflicts WHERE event_id=?', (eid,)).fetchone():
                    # A new verified mapping may resolve a previously unmatched
                    # event. Its conservative quiet period starts on resolution.
                    changed = prior['project_id'] != event.get('project_id') or prior['reason'] != event.get('reason', '')
                    db.execute('UPDATE events SET project_id=?,reason=?,received_at=? WHERE event_id=?',
                               (event.get('project_id'), event.get('reason', ''),
                                now if changed else prior['received_at'], eid))
                continue
            db.execute('INSERT INTO events(event_id,form_id,marker,payload_hash,payload,submitted_at,received_at,project_id,reason) '
                       'VALUES(?,?,?,?,?,?,?,?,?)', (eid, form_id, event['marker'], sha, body,
                       event['submitted_at'], now, event.get('project_id'), event.get('reason', '')))
        db.execute("INSERT INTO metadata VALUES('last_success',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
        db.execute("INSERT INTO metadata VALUES('last_error','') ON CONFLICT(key) DO UPDATE SET value='' ")


def record_error(home, message):
    with transaction(home) as db:
        db.execute("INSERT INTO metadata VALUES('last_error',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(message)[:500],))


def claim(home, *, now=None, quiet_seconds=QUIET_SECONDS):
    """Atomically freeze one eligible book's complete batch; no time-based unlock."""
    now = time.time() if now is None else now
    if type(quiet_seconds) is not int or quiet_seconds < QUIET_SECONDS:
        raise QueueError('The quiet period must be at least three hours.')
    with transaction(home) as db:
        meta = dict(db.execute('SELECT key,value FROM metadata').fetchall())
        if meta.get('last_error') or now - float(meta.get('last_success', 0)) > 900:
            return None
        # An unassigned submission might be another file for any book. Do not
        # start a new batch until its identity has been resolved explicitly.
        if db.execute('SELECT 1 FROM events LEFT JOIN books USING(project_id) '
                      'WHERE events.batch_id IS NULL AND books.project_id IS NULL').fetchone():
            return None
        for row in db.execute('SELECT * FROM books ORDER BY project_id').fetchall():
            pid = row['project_id']
            if row['blocked'] or db.execute("SELECT 1 FROM batches WHERE project_id=? AND state NOT IN ('delivered','released')", (pid,)).fetchone():
                continue
            pending = db.execute('SELECT * FROM events WHERE project_id=? AND batch_id IS NULL ORDER BY submitted_at,event_id', (pid,)).fetchall()
            pending = [e for e in pending if not db.execute('SELECT 1 FROM history WHERE project_id=? AND marker=?', (pid, e['marker'])).fetchone()]
            if not pending or any(e['reason'] for e in pending):
                continue
            if db.execute("SELECT 1 FROM history WHERE project_id=? AND state!='delivered'", (pid,)).fetchone():
                continue
            ready_at = max(max(e['received_at'], e['submitted_at']) for e in pending) + quiet_seconds
            if now < ready_at:
                continue
            ids = [e['event_id'] for e in pending]
            bid = hashlib.sha256(canonical({'project_id': pid, 'events': ids}).encode()).hexdigest()[:24]
            batch = {'batch_id': bid, 'project_id': pid, 'book': json.loads(row['data']),
                     'event_ids': ids, 'events': [json.loads(e['payload']) for e in pending],
                     'ready_at': ready_at, 'created_at': now}
            db.execute('INSERT INTO batches(batch_id,project_id,data,state,created_at) VALUES(?,?,?,?,?)',
                       (bid, pid, canonical(batch), 'claimed', now))
            db.executemany('UPDATE events SET batch_id=? WHERE event_id=?', [(bid, eid) for eid in ids])
            return batch
    return None


def pending_batches(home):
    with transaction(home) as db:
        return [{**json.loads(row['data']), 'state': row['state'], 'reason': row['reason'], 'job_id': row['job_id']}
                for row in db.execute("SELECT * FROM batches WHERE state IN ('claimed','running','delivery','awaiting_attachment') ORDER BY created_at")]


def assert_active(home, batch_id):
    with transaction(home) as db:
        row = db.execute('SELECT batches.state,books.blocked FROM batches JOIN books USING(project_id) WHERE batch_id=?', (batch_id,)).fetchone()
        if not row or row['state'] in {'held', 'delivered', 'local_complete'} or row['blocked']:
            raise QueueError('This batch is blocked or already completed; processing stopped.')


def set_batch(home, batch_id, state, *, reason='', job_id=''):
    if state not in {'running', 'delivery', 'awaiting_attachment', 'held', 'local_complete', 'delivered'}:
        raise QueueError('Invalid batch outcome.')
    with transaction(home) as db:
        old = db.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not old:
            raise QueueError('Unknown correction batch.')
        if old['state'] == 'held' and state != 'held':
            raise QueueError('A held batch needs an explicit reviewed recovery.')
        db.execute('UPDATE batches SET state=?,reason=?,job_id=? WHERE batch_id=?', (state, reason[:500], job_id or old['job_id'], batch_id))


def advance_book(home, batch_id, source_id, version):
    """Advance only after a verified delivery, and only from the frozen head."""
    with transaction(home) as db:
        batch_row = db.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not batch_row or batch_row['state'] == 'held':
            raise QueueError('This batch cannot advance the book source.')
        batch = json.loads(batch_row['data'])
        row = db.execute('SELECT * FROM books WHERE project_id=?', (batch['project_id'],)).fetchone()
        book = json.loads(row['data'])
        if book['source_id'] == source_id and book['source_version'] == version:
            return
        if row['blocked'] or book != batch['book'] or version != book['source_version'] + 1:
            raise QueueError('The registered book changed before delivery completed.')
        book.update(source_id=source_id, source_version=version)
        db.execute('UPDATE books SET data=? WHERE project_id=?', (canonical(book), book['project_id']))


def resume_delivery(home, batch_id):
    """Explicitly release a verified local result; never retry a held edit."""
    with transaction(home) as db:
        row = db.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not row or row['state'] != 'local_complete':
            raise QueueError('Only a verified local result can be released for delivery. Held edits require reviewed recovery.')
        batch = json.loads(row['data'])
        book = db.execute('SELECT * FROM books WHERE project_id=?', (batch['project_id'],)).fetchone()
        if not book or book['blocked'] or json.loads(book['data']) != batch['book']:
            raise QueueError('This book changed or is blocked. Review it before delivery.')
        db.execute("UPDATE batches SET state='delivery',reason='Explicitly released for verified delivery.' WHERE batch_id=?", (batch_id,))


def status(home, *, now=None, quiet_seconds=QUIET_SECONDS):
    now = time.time() if now is None else now
    if not (Path(home) / DB_NAME).exists():
        return {'quiet_seconds': quiet_seconds, 'events': [], 'batches': [], 'books': [], 'last_success': None, 'error': None}
    with transaction(home) as db:
        rows = db.execute('SELECT event_id,marker,project_id,submitted_at,received_at,reason,batch_id FROM events ORDER BY received_at DESC').fetchall()
        batches = [dict(row) for row in db.execute('SELECT batch_id,project_id,state,reason,created_at,job_id FROM batches ORDER BY created_at DESC')]
        mappings = [{**json.loads(row['data']), 'blocked': row['blocked']} for row in db.execute('SELECT * FROM books')]
        meta = dict(db.execute('SELECT key,value FROM metadata').fetchall())
        history = {(row['project_id'], row['marker']): row['state'] for row in db.execute('SELECT * FROM history')}
        events = [dict(row) for row in rows]
        latest = {}
        for event in events:
            event['history_state'] = history.get((event['project_id'], event['marker']))
            if not event['batch_id'] and not event['history_state']:
                pid = event['project_id']
                latest[pid] = max(latest.get(pid, 0), event['submitted_at'], event['received_at'])
        for event in events:
            event['ready_at'] = latest[event['project_id']] + quiet_seconds if not event['batch_id'] and not event['history_state'] else None
            if event['history_state'] == 'held':
                event['reason'] = 'An earlier worker already started this submission; reviewed recovery is required.'
        return {'quiet_seconds': quiet_seconds, 'events': events, 'batches': batches, 'books': mappings,
                'last_success': float(meta['last_success']) if meta.get('last_success') else None,
                'error': meta.get('last_error') or None}
