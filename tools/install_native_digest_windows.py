"""Install the daily digest separately from the active InDesign worker."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from install_native_interior_windows import task_xml

TASK_NAME = 'DocProof Interior Daily Digest'


def launcher_source(home, repo):
    return '''import importlib.util,os,sys
from pathlib import Path
home=Path(%r)
repo=Path(%r)
sys.path.insert(0,str(repo))
os.environ['PYTHONPATH']=str(repo)
home.joinpath('logs').mkdir(exist_ok=True)
sys.stdout=sys.stderr=open(home/'logs/daily-digest.log','a',encoding='utf-8',buffering=1)
spec=importlib.util.spec_from_file_location('desktop_setup',repo/'tools/windows_interior.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module.configure(home)
from docproof.interior.daily_digest import main
raise SystemExit(main(['--watch-home',str(home/'watch'),'--continuous']))
''' % (str(home), str(repo))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--recipient', required=True)
    parser.add_argument('--time', default='17:00')
    parser.add_argument('--timezone', default='America/New_York')
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        raise SystemExit('This installer requires Windows.')
    from docproof.interior.daily_digest import _read, validate_config
    from docproof.interior.workflow import save_json
    import win32api, win32security
    home = args.home.resolve()
    config_path = home / 'watch/daily-digest/config.json'
    old = _read(config_path)
    config = validate_config({'enabled':True, 'recipient':args.recipient, 'time':args.time,
        'timezone':args.timezone, 'armed_at':old.get('armed_at') or datetime.now(timezone.utc).isoformat()})
    startup = home / 'startup'
    startup.mkdir(parents=True, exist_ok=True)
    launcher = startup / 'digest-launch.py'
    launcher.write_text(launcher_source(home, Path(__file__).resolve().parents[1]), encoding='utf-8')
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    finally:
        token.Close()
    xml = startup / 'digest.xml'
    xml.write_text(task_xml(Path(sys.executable).with_name('pythonw.exe'), launcher, 'digest', home, sid), encoding='utf-16')
    if args.install:
        subprocess.run(['schtasks.exe','/Create','/TN',TASK_NAME,'/XML',str(xml),'/F'],
                       check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        save_json(config_path, config)
        subprocess.run(['schtasks.exe','/Run','/TN',TASK_NAME], check=True,
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps({'task':TASK_NAME,'installed':args.install,'recipient':args.recipient,
                      'time':args.time,'timezone':args.timezone,'existing_worker_unchanged':True}))


if __name__ == '__main__':
    main()
