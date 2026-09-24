"""Install this checkout's Mac correction worker: local review mode unless opted into delivery."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import plistlib
import shutil
import subprocess
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--install', action='store_true', help='Install and start the generated login agents')
    parser.add_argument('--enable-delivery', action='store_true',
                        help='Stage the worker without --local-only; refused unless saved settings allow delivery')
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    home = args.home.resolve()
    logs = home / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    python = repo / '.venv' / 'bin' / 'python'
    if sys.platform != 'darwin' or not python.is_file():
        raise SystemExit('This installer needs macOS and this checkout’s Python environment.')
    tool = [str(python), str(repo / 'tools' / 'mac_interior.py')]
    if args.enable_delivery:
        # Check the saved settings now, not at the first login, and change nothing.
        sys.path.insert(0, str(repo / 'tools'))
        from windows_interior import _poll_args
        _poll_args(home, enable_delivery=True)
    commands = {
        'com.docproof.interior-review-worker': [*tool, 'poll', '--home', str(home),
            *(['--enable-delivery'] if args.enable_delivery else [])],
        'com.docproof.interior-review-ui': [*tool, 'ui', '--home', str(home), '--port', str(args.port)],
    }
    # The website's on/off switch and status only once this Mac is paired
    # (`mac_interior.py pair`); an unpaired worker keeps its local settings.
    if (home / 'watch' / 'interior-remote' / 'config.json').is_file():
        commands['com.docproof.interior-website'] = [*tool, 'sync', '--home', str(home)]
    staged = home / 'launch-agents'
    staged.mkdir(exist_ok=True)
    for label, command in commands.items():
        config = {
            'Label': label, 'ProgramArguments': command,
            'WorkingDirectory': str(repo), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 30,
            'EnvironmentVariables': {'PATH': str(python.parent) + ':/opt/homebrew/bin:/Applications/ChatGPT.app/Contents/Resources:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                                     'PYTHONUNBUFFERED': '1'},
            'StandardOutPath': str(logs / (label + '.log')),
            'StandardErrorPath': str(logs / (label + '.error.log')),
        }
        path = staged / (label + '.plist')
        path.write_bytes(plistlib.dumps(config))
        if args.install:
            target = Path.home() / 'Library' / 'LaunchAgents' / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = staged / (target.name + '.' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.bak')
                shutil.copy2(target, backup)
                subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
            shutil.copy2(path, target)
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(target)], check=True)
            print(f'Started {label}.')
        else:
            print(path)


if __name__ == '__main__':
    main()
