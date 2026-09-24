"""Local Mac controls for the native corrections computer, with opt-in real
InDesign/Astra rehearsals. The Mac counterpart of windows_interior.py."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import urlsplit

from windows_interior import _poll_args, rehearsal

SITE = 'https://atmosphere-docproof.fly.dev'


def configure(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    from docproof.platform_io import private_path
    private_path(home, 0o700)
    os.environ['GALLEY_CODEX_HOME'] = str(home / 'codex')
    os.environ['DOCPROOF_HOME'] = str(home)
    # This dedicated computer uses the HubSpot connection saved in its own
    # Keychain, even if a shell inherited a token for another HubSpot app.
    os.environ.pop('HUBSPOT_TOKEN', None)
    # launchd starts agents with a bare PATH; Homebrew holds codex and Poppler.
    path = os.environ.get('PATH', '').split(os.pathsep)
    for extra in ('/opt/homebrew/bin', '/usr/local/bin'):
        if extra not in path:
            path.append(extra)
    os.environ['PATH'] = os.pathsep.join(path)


def run_jsx(script: Path):
    """Run one ExtendScript file in InDesign through Apple Events."""
    from docproof.interior.native import BUNDLE_ID
    tell = (f'tell application id {json.dumps(BUNDLE_ID)} to do script '
            f'(POSIX file {json.dumps(str(script), ensure_ascii=True)}) language javascript')
    return subprocess.run(['osascript', '-e', tell], capture_output=True, encoding='utf-8', timeout=90)


def pair(home: Path, site: str, *, replace: bool = False) -> dict:
    """Give this Mac a device ID and a fresh website secret in its Keychain.

    The secret never leaves the Keychain here: the same value must be set as
    Fly's DOCPROOF_INTERIOR_TOKEN, which `fly_secret_command` reads back out.
    """
    parsed = urlsplit(site)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise SystemExit('The corrections website must be a plain HTTPS address.')
    import keyring
    from docproof.interior.remote_control import read
    from docproof.interior.remote_sync import SERVICE
    from docproof.interior.workflow import save_json
    config_path = home / 'watch/interior-remote/config.json'
    existing = read(config_path)
    if existing and not replace:
        raise SystemExit('This computer is already paired; pass --replace to pair it again.')
    if existing.get('device_id'):
        try:
            keyring.delete_password(SERVICE, existing['device_id'])
        except keyring.errors.PasswordDeleteError:
            pass
    device_id = 'mac-' + secrets.token_urlsafe(24)
    keyring.set_password(SERVICE, device_id, secrets.token_urlsafe(48))
    config = {'url': site.rstrip('/'), 'device_id': device_id}
    save_json(config_path, config)
    return config


def fly_secret_command(device_id: str, app: str = 'atmosphere-docproof') -> str:
    """A shell line that copies the Keychain secret into Fly without echoing it."""
    from docproof.interior.remote_sync import SERVICE
    return (f"printf 'DOCPROOF_INTERIOR_TOKEN=%s\\n' \"$(security find-generic-password "
            f"-s {SERVICE} -a {device_id} -w)\" | fly secrets import -a {app}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['doctor', 'native-test', 'astra-test', 'login', 'ui',
                                           'poll', 'pair', 'sync'])
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--site', default=SITE, help='The corrections website (pair only)')
    parser.add_argument('--replace', action='store_true', help='Pair again with a new device ID (pair only)')
    parser.add_argument('--enable-delivery', action='store_true',
                        help='Opt into delivery only when saved native settings allow it')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This tool drives InDesign on macOS; use windows_interior.py on Windows.')
    if args.enable_delivery and args.action != 'poll':
        parser.error('--enable-delivery is valid only for poll')
    home = args.home.expanduser().resolve()
    configure(home)
    if args.action == 'doctor':
        from docproof.interior.__main__ import main as interior_main
        raise SystemExit(interior_main(['doctor']))
    elif args.action in ('native-test', 'astra-test'):
        from app.lock import FolderLock
        with FolderLock(home / 'rehearsal-owner'):
            rehearsal(home, astra=args.action == 'astra-test', run_jsx=run_jsx, label='Mac')
    elif args.action == 'login':
        from galley.codex_runner import codex_home, _private_dir, child_env, _binary, _AUTH_OPTIONS
        cache = _private_dir(codex_home())
        raise SystemExit(subprocess.call([_binary(None), *_AUTH_OPTIONS, 'login'], env=child_env(cache), cwd=cache))
    elif args.action == 'ui':
        from app.main import create_app
        import uvicorn
        # This panel never starts unrelated proofing/formatting/marketing clocks.
        uvicorn.run(create_app(home, start_runner=False), host='127.0.0.1', port=args.port)
    elif args.action == 'poll':
        from docproof.interior.__main__ import main as interior_main
        raise SystemExit(interior_main(_poll_args(home, enable_delivery=args.enable_delivery)))
    elif args.action == 'pair':
        config = pair(home, args.site, replace=args.replace)
        print(json.dumps({'paired': True, **config}))
        print('Set the same secret on the website, then install the login agents:')
        print('  ' + fly_secret_command(config['device_id']))
    elif args.action == 'sync':
        from docproof.interior.remote_sync import main as sync_main
        raise SystemExit(sync_main(['--watch-home', str(home / 'watch'), '--continuous']))


if __name__ == '__main__':
    main()
