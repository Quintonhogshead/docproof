"""The Mac corrections computer: pairing, rehearsal transport and login agents."""
import importlib.util
import json
from pathlib import Path
import plistlib
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


def _load_tool(filename, name, monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeKeyring:
    def __init__(self):
        self.store = {}
        self.errors = type('errors', (), {'PasswordDeleteError': KeyError})

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        del self.store[(service, user)]


@pytest.fixture
def keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, 'keyring', fake)
    return fake


def test_pair_writes_url_and_device_but_keeps_the_secret_in_the_keychain(tmp_path, monkeypatch, keyring):
    module = _load_tool('mac_interior.py', 'mac_interior_pair', monkeypatch)
    config = module.pair(tmp_path, 'https://atmosphere-docproof.fly.dev/')
    saved = json.loads((tmp_path / 'watch/interior-remote/config.json').read_text())
    assert saved == config == {'url': 'https://atmosphere-docproof.fly.dev',
                               'device_id': config['device_id']}
    secret = keyring.get_password('docproof-interior-computer', config['device_id'])
    assert len(secret) >= 32 and secret not in json.dumps(saved)
    # The website's heartbeat contract accepts this device ID.
    import re
    assert re.fullmatch(r'[A-Za-z0-9_-]{16,80}', config['device_id'])
    command = module.fly_secret_command(config['device_id'])
    assert secret not in command and 'fly secrets import -a atmosphere-docproof' in command


def test_pair_refuses_to_silently_replace_a_pairing(tmp_path, monkeypatch, keyring):
    module = _load_tool('mac_interior.py', 'mac_interior_repair', monkeypatch)
    first = module.pair(tmp_path, 'https://atmosphere-docproof.fly.dev')
    with pytest.raises(SystemExit, match='already paired'):
        module.pair(tmp_path, 'https://atmosphere-docproof.fly.dev')
    second = module.pair(tmp_path, 'https://atmosphere-docproof.fly.dev', replace=True)
    assert second['device_id'] != first['device_id']
    assert keyring.get_password('docproof-interior-computer', first['device_id']) is None


@pytest.mark.parametrize('site', ['http://atmosphere-docproof.fly.dev',
                                  'https://user:pw@atmosphere-docproof.fly.dev',
                                  'https://atmosphere-docproof.fly.dev/api'])
def test_pair_needs_a_plain_https_site(tmp_path, monkeypatch, keyring, site):
    module = _load_tool('mac_interior.py', 'mac_interior_site', monkeypatch)
    with pytest.raises(SystemExit, match='plain HTTPS'):
        module.pair(tmp_path, site)
    assert not (tmp_path / 'watch/interior-remote/config.json').exists()


def test_mac_rehearsal_runs_its_script_through_apple_events(tmp_path, monkeypatch):
    module = _load_tool('mac_interior.py', 'mac_interior_jsx', monkeypatch)
    seen = []
    monkeypatch.setattr(module.subprocess, 'run', lambda argv, **kw: seen.append((argv, kw)))
    module.run_jsx(tmp_path / 'with "quotes" and spaces.jsx')
    argv, options = seen[0]
    assert argv[:2] == ['osascript', '-e']
    assert 'tell application id "com.adobe.InDesign" to do script' in argv[2]
    assert json.dumps(str(tmp_path / 'with "quotes" and spaces.jsx')) in argv[2]
    assert options['timeout'] == 90


needs_venv = pytest.mark.skipif(not (TOOLS.parent / '.venv/bin/python').is_file(),
                                reason="the installer runs this checkout's .venv")


def _install(module, home, monkeypatch, *extra):
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    module.main(['--home', str(home), *extra])
    return {path.stem: plistlib.loads(path.read_bytes())
            for path in (home / 'launch-agents').glob('*.plist')}


@needs_venv
def test_installer_stages_local_only_worker_and_no_website_agent_before_pairing(tmp_path, monkeypatch):
    module = _load_tool('install_native_interior.py', 'mac_installer_local', monkeypatch)
    agents = _install(module, tmp_path, monkeypatch)
    assert set(agents) == {'com.docproof.interior-review-worker', 'com.docproof.interior-review-ui'}
    worker = agents['com.docproof.interior-review-worker']['ProgramArguments']
    assert worker[1].endswith('tools/mac_interior.py') and worker[2:] == ['poll', '--home', str(tmp_path)]
    assert agents['com.docproof.interior-review-ui']['ProgramArguments'][2] == 'ui'


@needs_venv
def test_installer_adds_website_agent_once_paired(tmp_path, monkeypatch):
    module = _load_tool('install_native_interior.py', 'mac_installer_paired', monkeypatch)
    remote = tmp_path / 'watch/interior-remote'
    remote.mkdir(parents=True)
    (remote / 'config.json').write_text('{"url": "https://example.test", "device_id": "mac-0123456789abcdef"}')
    agents = _install(module, tmp_path, monkeypatch)
    assert agents['com.docproof.interior-website']['ProgramArguments'][2:] == ['sync', '--home', str(tmp_path)]


@needs_venv
def test_installer_delivery_needs_saved_settings_and_changes_none(tmp_path, monkeypatch):
    module = _load_tool('install_native_interior.py', 'mac_installer_delivery', monkeypatch)
    with pytest.raises(SystemExit, match='saved corrections_native_auto_upload'):
        _install(module, tmp_path, monkeypatch, '--enable-delivery')
    from app.watch.settings import WatchSettings
    WatchSettings(corrections_native_auto_upload=True, corrections_native_form_poll=True).save(tmp_path / 'watch')
    agents = _install(module, tmp_path, monkeypatch, '--enable-delivery')
    assert agents['com.docproof.interior-review-worker']['ProgramArguments'][-1] == '--enable-delivery'
