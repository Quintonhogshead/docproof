"""One deterministic daily email from native queue receipts, independent of InDesign."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading
from zoneinfo import ZoneInfo

from app.lock import FolderInUse, FolderLock
from .workflow import save_json


def _read(path, default=None):
    if not path.is_file():
        return {} if default is None else default
    value = json.loads(path.read_text('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid digest record')
    return value


def validate_config(config):
    if not isinstance(config.get('enabled', False), bool):
        raise ValueError('Digest enabled must be a boolean')
    if not config.get('enabled'):
        return config
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", config.get('recipient', '')):
        raise ValueError('Choose one valid daily digest email address')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', config.get('time', '')):
        raise ValueError('Daily digest time must be HH:MM')
    ZoneInfo(config['timezone'])
    armed = datetime.fromisoformat(config['armed_at'])
    if armed.tzinfo is None:
        raise ValueError('Daily digest activation time needs a time zone')
    return config


def slot(config, state, now):
    """Latest due slot, collapsing sleep gaps to one catch-up email."""
    local = now.astimezone(ZoneInfo(config['timezone']))
    hour, minute = map(int, config['time'].split(':'))
    today = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    latest = today if today <= local else today - timedelta(days=1)
    floor = datetime.fromisoformat(state.get('covered_schedule_through') or config['armed_at'])
    due = latest.astimezone(timezone.utc) > floor.astimezone(timezone.utc)
    next_at = today if today > local else today + timedelta(days=1)
    # A catch-up received this morning counts as today's one email too.
    if state.get('last_attempt_day') == local.date().isoformat():
        due = False
        next_at = today + timedelta(days=1)
    return {'due': due, 'day': local.date().isoformat(),
            'covered_through': today.isoformat(), 'next_at': next_at.isoformat()}


def _label(book, fallback):
    # Editorial/user text is body data, never an email header or command.
    clean = lambda value: ' '.join(str(value or '').split())
    return ' — '.join(filter(None, [clean(book.get('author')), clean(book.get('title'))])) or fallback


def snapshot(home):
    from app.watch import native_queue as queue
    from app.watch.settings import WatchSettings
    ws = WatchSettings.load(home)
    status = queue.status(home, quiet_seconds=ws.corrections_native_quiet_seconds)
    jobs = {}
    for batch in status['batches']:
        jid = batch.get('job_id', '')
        if re.fullmatch(r'[a-f0-9]{24}', jid):
            jobs[jid] = _read(home / 'native_jobs' / jid / 'job.json')
    status['jobs'] = jobs
    status['worker'] = _read(home / 'native-worker.json')
    status['corrections_enabled'] = ws.corrections_enabled
    return status


def compose(data, sent_batches, now, config):
    books = {row['project_id']: row for row in data['books']}
    completed, review, running, pending = [], [], [], Counter()
    newly_reported = []
    for batch in data['batches']:
        label = _label(books.get(batch['project_id'], {}), 'Project ' + batch['project_id'])
        status, bid = batch['state'], batch['batch_id']
        job = data.get('jobs', {}).get(batch.get('job_id'), {})
        if status == 'delivered' and bid not in sent_batches:
            newly_reported.append(bid)
            lines = [label]
            counts = (job.get('result') or {}).get('counts') or {}
            if type(counts.get('edits')) is int:
                lines.append(f"  {counts['edits']} edits; {counts.get('unresolved', 0)} unresolved requests")
            for name, fid in sorted(job.get('uploaded', {}).items()):
                if name.endswith(('.indd', '.corrections.xlsx')) and re.fullmatch(r'[\w-]+', str(fid), re.ASCII):
                    lines.append(f"  {' '.join(name.split())}: https://drive.google.com/file/d/{fid}/view")
            completed.append('\n'.join(lines))
        elif status in {'held', 'awaiting_attachment', 'local_complete'}:
            reason = {'held': 'Needs review; automatic processing is held.',
                      'awaiting_attachment': 'Waiting for all submitted files.',
                      'local_complete': 'Verified locally; delivery has not been released.'}[status]
            # Provider exception bodies and submitted correction text never
            # enter the digest. Exact diagnostics stay in the local panel.
            review.append(f'{label}: {reason}')
        elif status in {'claimed', 'running', 'delivery'}:
            running.append(f"{label}: {'delivery pending' if status == 'delivery' else 'processing'}")
    unmatched = 0
    for event in data['events']:
        if event.get('batch_id') or event.get('history_state') == 'delivered':
            continue
        if event.get('reason') or event.get('history_state') == 'held':
            unmatched += 1
        else:
            pending[_label(books.get(event.get('project_id'), {}), 'Unmapped book')] += 1
    if unmatched:
        review.append(f'{unmatched} submission(s) need a book match or intake review in the local panel.')
    worker = data.get('worker') or {}
    system = []
    if not data.get('corrections_enabled', True):
        system.append('Corrections processing is paused.')
    if data.get('error'):
        system.append('The latest form collection failed; check the local panel and saved connections.')
    if worker.get('state') == 'error' or (worker.get('report') or {}).get('failed'):
        system.append('The corrections worker reported a processing failure; check the local panel.')
    stamp = worker.get('finished_at') or worker.get('started_at')
    if not stamp:
        system.append('No worker check has been recorded yet.')
    else:
        age = now - datetime.fromisoformat(stamp)
        if worker.get('state') != 'checking' and age.total_seconds() > 1200:
            system.append('The worker has not reported a recent completed check.')
    local = now.astimezone(ZoneInfo(config['timezone']))
    parts = [f"DocProof daily corrections summary — {local:%B %d, %Y}",
             f"As of {local:%I:%M %p %Z}. Completed books are those not included in a previously confirmed digest."]
    for title, rows in [('Completed', completed), ('Needs review', review),
                        ('In progress', running),
                        ('Waiting for the quiet period / next queue pass', [f'{name}: {count} submission(s)' for name, count in sorted(pending.items())]),
                        ('System issues', system)]:
        parts.extend(['', f'{title} ({len(rows)})', '\n'.join('- ' + row for row in rows) or 'None.'])
    parts.extend(['', 'Review holds and failures in the DocProof panel on the laptop.',
                  'This is the single daily digest; no per-book email is sent.'])
    return {'subject': f'[DocProof] Daily corrections — {local:%Y-%m-%d}',
            'body': '\n'.join(parts), 'batch_ids': newly_reported,
            'counts': {'completed': len(completed), 'review': len(review), 'in_progress': len(running),
                       'waiting_books': len(pending), 'system_issues': len(system)}}


def access_token(home, *, get_key=None, opener=None):
    from app.settings import get_api_key
    from app.watch import drive
    from app.watch.settings import WatchSettings
    ws = WatchSettings.load(home)
    refresh = (get_key or get_api_key)('google')
    if not refresh or not ws.client_id or not ws.client_secret:
        raise ValueError('Google connection is missing')
    return drive.refresh_access_token(ws.client_id, ws.client_secret, refresh,
                                      opener=opener or drive._open_url)


def send(token, recipient, message, *, opener=None):
    from app.watch import drive, notify
    payload = json.dumps({'raw': notify._raw(recipient, message['subject'], message['body'])}).encode()
    result = drive._json_call(drive._request(notify.SEND_URL, token, data=payload,
        method='POST', content_type='application/json'), opener=opener or drive._open_url,
        what='send the daily corrections digest')
    if not isinstance(result.get('id'), str) or not result['id']:
        raise ValueError('Gmail did not return a message receipt')
    return result['id']


def poll(home, *, now=None, get_key=None, opener=None, sender=None, snapshotter=None):
    home = Path(home)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('An aware current time is required')
    root = home / 'daily-digest'
    config = validate_config(_read(root / 'config.json'))
    def status(state, **extra):
        value = {'state': state, 'checked_at': now.isoformat(), **extra}
        save_json(root / 'status.json', value)
        return value
    try:
        with FolderLock(root):
            if not config.get('enabled'):
                return status('disabled')
            state = _read(root / 'state.json')
            last = _read(root / 'outbox' / (state.get('last_attempt_day', 'none') + '.json'))
            if last.get('state') == 'sent':
                # Recover a crash after Gmail's receipt was persisted but
                # before the summary watermark was saved.
                state.update(last_sent_at=last['sent_at'], sent_batches=sorted(
                    set(state.get('sent_batches', [])) | set(last['batch_ids'])))
                save_json(root / 'state.json', state)
            timing = slot(config, state, now)
            info = {'recipient': config['recipient'], 'time': config['time'], 'timezone': config['timezone'],
                    'next_at': timing['next_at'], 'last_sent_at': state.get('last_sent_at')}
            prior = _read(root / 'outbox' / (timing['day'] + '.json'))
            if prior.get('state') == 'sent':
                return status('scheduled', **{**info, 'last_sent_at':prior['sent_at']})
            if prior.get('state') in {'sending', 'uncertain'}:
                return status('delivery_uncertain', **info,
                    message='A previous send may have reached Gmail; it will not be repeated today.')
            if not timing['due']:
                return status('scheduled', **info)
            try:
                token = access_token(home, get_key=get_key, opener=opener)
                message = compose((snapshotter or snapshot)(home), set(state.get('sent_batches', [])), now, config)
            except Exception as exc:
                return status('preparation_failed', **info, error_type=type(exc).__name__,
                              message='The digest could not read its queue or Google connection. It will retry before sending.')
            outbox = root / 'outbox' / (timing['day'] + '.json')
            attempt = {'state': 'sending', 'attempted_at': now.isoformat(),
                       'recipient': config['recipient'], **message}
            # Persist the attempt BEFORE the network boundary. A crash, timeout
            # or missing receipt must never trigger an automatic second email.
            save_json(outbox, attempt)
            state.update(last_attempt_day=timing['day'], covered_schedule_through=timing['covered_through'])
            save_json(root / 'state.json', state)
            try:
                message_id = (sender or send)(token, config['recipient'], message, opener=opener)
                if not message_id:
                    raise ValueError('Missing message receipt')
            except Exception as exc:
                attempt.update(state='uncertain', error_type=type(exc).__name__)
                save_json(outbox, attempt)
                return status('delivery_uncertain', **info, error_type=type(exc).__name__,
                    message='Gmail delivery could not be confirmed. No second email will be attempted today.')
            attempt.update(state='sent', message_id=message_id, sent_at=now.isoformat())
            save_json(outbox, attempt)
            state.update(last_sent_at=now.isoformat(),
                sent_batches=sorted(set(state.get('sent_batches', [])) | set(message['batch_ids'])))
            save_json(root / 'state.json', state)
            return status('sent', **{**info, 'last_sent_at': now.isoformat()}, counts=message['counts'])
    except FolderInUse:
        return {'state': 'busy'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch-home', type=Path, required=True)
    parser.add_argument('--continuous', action='store_true')
    args = parser.parse_args(argv)
    stop = threading.Event()
    while True:
        try:
            result = poll(args.watch_home)
        except Exception as exc:
            result = {'state':'error', 'error_type':type(exc).__name__,
                      'checked_at':datetime.now(timezone.utc).isoformat()}
            save_json(args.watch_home / 'daily-digest/status.json', result)
        print(json.dumps(result), flush=True)
        if not args.continuous or stop.wait(60):
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
