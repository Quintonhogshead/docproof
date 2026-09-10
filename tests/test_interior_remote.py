from datetime import datetime, timedelta, timezone
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.main import create_app
from app.settings import Paths
from app.watch import interior_remote as remote
from app.watch.settings import WatchSettings
from docproof.interior import remote_control as control, remote_sync as sync
from docproof.interior.workflow import save_json

TOKEN = 'a-dedicated-interior-token-at-least-32-characters'
DEVICE = 'windows-test-device-0001'


def beat(**changes):
    value = dict(device_id=DEVICE, applied_revision=0, enabled=True, configured_enabled=True,
                 quiet_seconds=10800, auto_upload=True, worker={'state':'idle'}, digest={}, counts={})
    return {**value, **changes}


@pytest.fixture
def app(tmp_path, monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, 'get_password', lambda *args: None)
    monkeypatch.setenv(remote.TOKEN_ENV, TOKEN)
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user('boss@example.com', 'password1', is_admin=True)
    accounts.create_user('reader@example.com', 'password1', is_admin=False)
    return create_app(tmp_path, start_runner=False, web=True,
                      session_secret='test-session-secret', https_only=False)


def boss(app):
    client = TestClient(app)
    assert client.post('/api/login', json={'email':'boss@example.com','password':'password1'}).status_code == 200
    return client


def post(app, data=None):
    return TestClient(app).post(remote.ROUTE, json=data or beat(), headers={'Authorization':'Bearer ' + TOKEN})


def test_switch_controls_computer_and_requires_ack_without_enabling_fly_worker(app):
    ws = WatchSettings.load(app.state.watch.home)
    ws.corrections_enabled = True
    ws.proofing_enabled = True
    ws.save(app.state.watch.home)
    assert post(app).json() == {'enabled':True,'revision':1}
    client = boss(app)
    panel = client.get('/api/watch').json()['watch']
    assert panel['interior_computer']['pending'] and panel['corrections_enabled']
    assert not WatchSettings.load(app.state.watch.home).corrections_enabled
    assert WatchSettings.load(app.state.watch.home).proofing_enabled
    post(app, beat(applied_revision=1))
    assert not client.get('/api/watch').json()['watch']['interior_computer']['pending']
    changed = client.put('/api/watch', json={'corrections_enabled':False}).json()['watch']
    assert not changed['corrections_enabled'] and changed['interior_computer']['pending']
    # An old heartbeat cannot undo an operator change.
    assert post(app, beat(applied_revision=1)).json() == {'enabled':False,'revision':2}
    post(app, beat(applied_revision=2, enabled=False))
    panel = client.get('/api/watch').json()['watch']
    assert not panel['interior_computer']['pending']
    assert client.put('/api/watch', json={'corrections_native_partial_upload':True}).status_code == 409
    assert not WatchSettings.load(app.state.watch.home).corrections_enabled


def test_auth_scope_body_bounds_and_single_device(app, monkeypatch):
    client = TestClient(app)
    assert client.post(remote.ROUTE, json=beat()).status_code == 401
    headers = {'Authorization':'Bearer ' + TOKEN}
    assert client.put(remote.ROUTE, headers=headers, json={'enabled':False}).status_code == 401
    assert client.put('/api/watch', headers=headers, json={'corrections_enabled':False}).status_code == 401
    assert client.post('/api/login', json={'email':'reader@example.com','password':'password1'}).status_code == 200
    assert client.put(remote.ROUTE, json={'enabled':False}).status_code == 403
    assert client.get('/api/watch').status_code == 403
    assert client.post(remote.ROUTE, headers=headers, content=b'x' * (remote.MAX_BYTES + 1)).status_code == 413
    response = post(app, beat(secret='must-not-echo'))
    assert response.status_code == 400 and 'must-not-echo' not in response.text
    assert post(app).status_code == 200
    assert post(app, beat(device_id='different-device-0002')).status_code == 409
    assert post(app, beat(applied_revision=999)).status_code == 409
    monkeypatch.delenv(remote.TOKEN_ENV)
    assert post(app).status_code == 403


def test_stale_heartbeat_and_separately_stale_mailer(app):
    now = datetime.now(timezone.utc)
    post(app, beat(digest={'state':'scheduled', 'checked_at':(now-timedelta(minutes=8)).isoformat()}))
    status = remote.status(app.state.watch.home, now=now+timedelta(seconds=1))
    assert not status['stale'] and status['digest']['stale']
    assert remote.status(app.state.watch.home, now=now+timedelta(seconds=121))['stale']
    assert 'device_id' not in status


def test_unpaired_unchanged_and_paired_fails_closed(tmp_path):
    assert control.allowed(tmp_path, now=100)
    save_json(tmp_path/'interior-remote/config.json', {'url':'https://example.com','device_id':DEVICE})
    assert not control.allowed(tmp_path, now=100)
    for lease in ({'enabled':False,'expires_at':190}, {'enabled':True,'expires_at':99},
                  {'enabled':True,'expires_at':999999}, {'enabled':'yes','expires_at':190}):
        save_json(tmp_path/'interior-remote/lease.json', lease)
        assert not control.allowed(tmp_path, now=100)
    save_json(tmp_path/'interior-remote/lease.json', {'enabled':True,'expires_at':190})
    assert control.allowed(tmp_path, now=100)


def test_sync_reaches_only_approved_endpoint_and_never_sends_email(tmp_path):
    WatchSettings(corrections_enabled=True, corrections_native_quiet_seconds=10800).save(tmp_path)
    save_json(tmp_path/'interior-remote/config.json', {'url':'https://example.com','device_id':DEVICE})
    save_json(tmp_path/'daily-digest/config.json', {'enabled':True,'recipient':'reader@example.com', 'time':'17:00','timezone':'America/New_York','private':'do-not-send'})
    save_json(tmp_path/'daily-digest/status.json', {'state':'scheduled','raw_token':'do-not-send'})
    requests = []
    def opener(request, **kwargs):
        requests.append(request)
        return io.BytesIO(b'{"enabled":false,"revision":3}')
    sync.sync_once(tmp_path, get_token=lambda:TOKEN, opener=opener, now=100)
    request = requests[0]
    assert request.full_url == 'https://example.com/api/watch/interior-computer'
    assert request.get_header('Authorization') == 'Bearer ' + TOKEN
    assert b'do-not-send' not in request.data and TOKEN.encode() not in request.data
    assert not control.allowed(tmp_path, now=100)
    lease = control.read(tmp_path/'interior-remote/lease.json')
    assert lease == {'enabled':False,'revision':3,'expires_at':190}
    with pytest.raises(ValueError, match='older'):
        sync.sync_once(tmp_path, get_token=lambda:TOKEN, opener=lambda *a, **k:io.BytesIO(b'{"enabled":true,"revision":2}'), now=101)
    assert control.read(tmp_path/'interior-remote/lease.json') == lease


def test_paused_poller_does_not_read_tokens_or_call_network(tmp_path):
    from docproof.interior import poller
    WatchSettings(corrections_enabled=True, corrections_engine='native').save(tmp_path)
    save_json(tmp_path/'interior-remote/config.json', {'url':'https://example.com'})
    def forbidden(*a, **k):
        pytest.fail('Paused worker reached an external boundary')
    poller.poll_once(tmp_path, get_key=forbidden, opener=forbidden)
    assert poller.collect_once(tmp_path, get_key=forbidden, opener=forbidden) == {'state':'paused'}
    assert control.read(tmp_path/'native-worker.json')['state'] == 'paused'


def test_real_control_round_trip_keeps_laptop_settings_and_email_unchanged(app, tmp_path):
    laptop = tmp_path / 'laptop'
    original = WatchSettings(corrections_enabled=True, corrections_engine='native',
                             corrections_native_auto_upload=True, corrections_native_quiet_seconds=10800)
    original.save(laptop)
    save_json(laptop/'interior-remote/config.json', {'url':'https://example.com','device_id':DEVICE})
    email = {'enabled':True,'recipient':'boss@example.com','time':'17:00','timezone':'America/New_York'}
    save_json(laptop/'daily-digest/config.json', email)
    def transport(request, **kwargs):
        response = TestClient(app).post(remote.ROUTE, content=request.data,
                     headers={'Authorization':request.get_header('Authorization')})
        assert response.status_code == 200, response.text
        return io.BytesIO(response.content)
    def exchange():
        return sync.sync_once(laptop, get_token=lambda:TOKEN, opener=transport)
    exchange(); exchange()
    assert control.allowed(laptop) and not remote.status(app.state.watch.home)['pending']
    client = boss(app)
    assert client.put('/api/watch', json={'corrections_enabled':False}).status_code == 200
    exchange(); exchange()
    assert not control.allowed(laptop) and not remote.status(app.state.watch.home)['pending']
    assert client.put('/api/watch', json={'corrections_enabled':True}).status_code == 200
    exchange(); exchange()
    assert control.allowed(laptop) and not remote.status(app.state.watch.home)['pending']
    assert WatchSettings.load(laptop) == original
    assert control.read(laptop/'daily-digest/config.json') == email
