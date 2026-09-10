"""Install only the corrections website connection; leave the mailer independent."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

from install_native_interior_windows import task_xml

TASK_NAME = 'DocProof Interior Website Connection'


def launcher_source(home, repo):
    return '''import os,sys
from pathlib import Path
home=Path(%r)
repo=Path(%r)
sys.path.insert(0,str(repo))
os.environ['PYTHONPATH']=str(repo)
home.joinpath('logs').mkdir(exist_ok=True)
sys.stdout=sys.stderr=open(home/'logs/interior-website.log','a',encoding='utf-8',buffering=1)
from docproof.interior.remote_sync import main
raise SystemExit(main(['--watch-home',str(home/'watch'),'--continuous']))
''' % (str(home), str(repo))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        raise SystemExit('Windows is required.')
    import win32api, win32security
    from docproof.interior.remote_control import read
    home = args.home.resolve()
    if args.install and not read(home / 'watch/interior-remote/config.json'):
        raise SystemExit('Pair the website before installing this task.')
    startup = home / 'startup'
    startup.mkdir(parents=True, exist_ok=True)
    launcher = startup / 'interior-website.py'
    launcher.write_text(launcher_source(home, Path(__file__).resolve().parents[1]), encoding='utf-8')
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    finally:
        token.Close()
    xml = startup / 'interior-website.xml'
    xml.write_text(task_xml(Path(sys.executable).with_name('pythonw.exe'), launcher, 'sync', home, sid), encoding='utf-16')
    if args.install:
        for command in ([ 'schtasks.exe', '/Create', '/TN', TASK_NAME, '/XML', str(xml), '/F'],
                        [ 'schtasks.exe', '/Run', '/TN', TASK_NAME]):
            subprocess.run(command, check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps({'task': TASK_NAME, 'installed': args.install}))


if __name__ == '__main__':
    main()
