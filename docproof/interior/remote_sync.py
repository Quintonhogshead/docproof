"""Small desktop heartbeat for the existing Fly automation switch and daily email."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from .remote_control import allowed, read
from .workflow import save_json

SERVICE = 'docproof-interior-computer'
INTERVAL = 15
LEASE_SECONDS = 90


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def snapshot(home, config):
    from app.watch.settings import WatchSettings
    from app.watch import native_queue
    ws = WatchSettings.load(home)
    root = Path(home)
    lease = read(root / 'interior-remote/lease.json')
    worker = read(root / 'native-worker.json')
    digest_config = read(root / 'daily-digest/config.json')
    digest_status = read(root / 'daily-digest/status.json')
    counts = dict(waiting=0, review=0, running=0, delivered=0)
    queue_error = False
    try:
        queue = native_queue.status(home, quiet_seconds=ws.corrections_native_quiet_seconds)
        counts['waiting'] = sum(not x.get('batch_id') and not x.get('history_state') for x in queue['events'])
        counts['review'] = sum(bool(x.get('reason')) and not x.get('batch_id') and not x.get('history_state') for x in queue['events'])
        for batch in queue['batches']:
            key = {'held':'review', 'awaiting_attachment':'review', 'delivered':'delivered',
                   'running':'running', 'delivery':'running'}.get(batch['state'])
            if key:
                counts[key] += 1
    except Exception:
        queue_error = True
    digest_state = digest_status.get('state', 'unknown')
    if digest_state not in {'disabled','scheduled','sent','delivery_uncertain','preparation_failed','error','unknown'}:
        digest_state = 'unknown'
    return {'device_id': config['device_id'], 'applied_revision': lease.get('revision', 0),
            'enabled': ws.corrections_enabled and allowed(home), 'configured_enabled': ws.corrections_enabled,
            'quiet_seconds': ws.corrections_native_quiet_seconds, 'auto_upload': ws.corrections_native_auto_upload,
            'worker': {key: worker.get(key) for key in ('state','started_at','finished_at')}
                      if worker.get('state') in {'paused','checking','idle','error','attention'} else {'state':'paused'},
            'digest': {'enabled': digest_config.get('enabled', False),
                       **{key: digest_config.get(key, '') for key in ('recipient','time','timezone')},
                       'state': digest_state,
                       **{key: digest_status.get(key) for key in ('checked_at','next_at','last_sent_at')}},
            'counts': counts, 'queue_error': queue_error}


def sync_once(home, *, get_token=None, opener=None, now=None):
    home = Path(home)
    config = read(home / 'interior-remote/config.json')
    url = config.get('url', '')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('The corrections website must use a plain HTTPS address.')
    if get_token is None:
        import keyring
        get_token = lambda: keyring.get_password(SERVICE, config['device_id'])
    token = get_token()
    if not token or len(token) < 32:
        raise ValueError('Corrections website connection is missing.')
    started = time.time() if now is None else now
    request = urllib.request.Request(url.rstrip('/') + '/api/watch/interior-computer',
        data=json.dumps(snapshot(home, config)).encode(), method='POST',
        headers={'Authorization':'Bearer ' + token, 'Content-Type':'application/json'})
    open_url = opener or urllib.request.build_opener(NoRedirect()).open
    with open_url(request, timeout=12) as response:
        raw = response.read(4097)
    if len(raw) > 4096:
        raise ValueError('Invalid website response size')
    desired = json.loads(raw)
    if (not isinstance(desired, dict) or set(desired) != {'enabled','revision'}
            or type(desired['enabled']) is not bool or type(desired['revision']) is not int
            or desired['revision'] < 1):
        raise ValueError('Invalid website control')
    prior = read(home / 'interior-remote/lease.json')
    if desired['revision'] < prior.get('revision', 0):
        raise ValueError('The website returned an older control revision')
    save_json(home / 'interior-remote/lease.json', {**desired, 'expires_at': started + LEASE_SECONDS})
    receipt = {'state':'connected', 'checked_at': datetime.now(timezone.utc).isoformat(), **desired}
    save_json(home / 'interior-remote/status.json', receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch-home', type=Path, required=True)
    parser.add_argument('--continuous', action='store_true')
    args = parser.parse_args(argv)
    from app.lock import FolderLock
    with FolderLock(args.watch_home / 'interior-remote/process'):
        stop = threading.Event()
        while True:
            try:
                result = sync_once(args.watch_home)
            except Exception:
                # Never log URLs, credentials or provider response bodies.
                result = {'state':'disconnected', 'checked_at':datetime.now(timezone.utc).isoformat()}
                save_json(args.watch_home / 'interior-remote/status.json', result)
            print(json.dumps(result), flush=True)
            if not args.continuous or stop.wait(INTERVAL):
                return 0


if __name__ == '__main__':
    raise SystemExit(main())
