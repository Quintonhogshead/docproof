"""Install local-review tasks in the signed-in Windows desktop session."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def task_xml(command: Path, launcher: Path, action: str, home: Path, user_sid: str) -> str:
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('',ns)
    def node(parent,name,text=None):
        element=ET.SubElement(parent,'{'+ns+'}'+name)
        if text is not None: element.text=text
        return element
    root=ET.Element('{'+ns+'}Task',{'version':'1.2'})
    trigger=node(node(root,'Triggers'),'LogonTrigger')
    node(trigger,'Enabled','true'); node(trigger,'UserId',user_sid)
    principal=node(node(root,'Principals'),'Principal'); principal.set('id','User')
    node(principal,'UserId',user_sid); node(principal,'LogonType','InteractiveToken')
    node(principal,'RunLevel','LeastPrivilege')
    settings=node(root,'Settings')
    for key,value in {'MultipleInstancesPolicy':'IgnoreNew','DisallowStartIfOnBatteries':'false',
                      'StopIfGoingOnBatteries':'false','StartWhenAvailable':'true',
                      'Enabled':'true','Hidden':'true','ExecutionTimeLimit':'PT0S'}.items():
        node(settings,key,value)
    restart=node(settings,'RestartOnFailure')
    node(restart,'Interval','PT1M'); node(restart,'Count','3')
    actions=node(root,'Actions'); actions.set('Context','User')
    executable=node(actions,'Exec')
    node(executable,'Command',str(command))
    node(executable,'Arguments',subprocess.list2cmdline([str(launcher),action]))
    node(executable,'WorkingDirectory',str(home))
    return ET.tostring(root,encoding='unicode')


def launcher_source(home: Path, tool: Path, port: int, path_value: str,
                    codex_bin: str, *, enable_delivery: bool = False) -> str:
    """Create the task launcher without putting paths through a shell."""
    return '''import os,runpy,sys
from pathlib import Path
home=Path(%r)
repo_root=Path(%r)
sys.path.insert(0,str(repo_root))
home.joinpath('logs').mkdir(exist_ok=True)
action=sys.argv[1]
sys.stdout=sys.stderr=open(home/'logs'/(action+'.log'),'a',encoding='utf-8',buffering=1)
os.environ['PATH']=%r
os.environ['GALLEY_CODEX_BIN']=%r
os.environ['PYTHONPATH']=str(repo_root) + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
sys.argv=[%r,action,'--home',str(home),'--port',%r] + (['--enable-delivery'] if action == 'poll' and %r else [])
runpy.run_path(sys.argv[0],run_name='__main__')
''' % (str(home), str(tool.parent.parent), path_value, codex_bin, str(tool), str(port), enable_delivery)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--install',action='store_true')
    parser.add_argument('--enable-delivery', action='store_true',
                        help='Stage delivery opt-in only when saved native settings allow it')
    args=parser.parse_args()
    if sys.platform!='win32': raise SystemExit('This installer requires Windows.')
    home=args.home.resolve()
    if args.enable_delivery:
        from app.watch.settings import WatchSettings
        ws=WatchSettings.load(home/'watch')
        if not ws.corrections_native_auto_upload or not ws.corrections_native_form_poll:
            raise SystemExit(
                'Delivery opt-in requires saved corrections_native_auto_upload and '
                'corrections_native_form_poll settings; no settings were changed.')
    import win32api,win32security
    token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32security.TOKEN_QUERY)
    try: sid=win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token,win32security.TokenUser)[0])
    finally: token.Close()
    staged=home/'startup'; staged.mkdir(parents=True,exist_ok=True)
    tool=Path(__file__).resolve().with_name('windows_interior.py')
    launcher=staged/'launch.py'
    # Only paths and runtime settings go in this launcher. Tokens stay in the
    # user's Credential Manager and the worker's private Codex cache.
    launcher.write_text(launcher_source(
        home, tool, args.port, os.environ.get('PATH',''),
        os.environ.get('GALLEY_CODEX_BIN',''), enable_delivery=args.enable_delivery),
        encoding='utf-8')
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file(): raise SystemExit('The Windows Python environment has no pythonw.exe.')
    for action,name in [('ui','DocProof Interior Review UI'),('poll','DocProof Interior Review Worker')]:
        xml=staged/(action+'.xml')
        xml.write_text(task_xml(pythonw,launcher,action,home,sid),encoding='utf-16')
        if args.install:
            subprocess.run(['schtasks.exe','/Create','/TN',name,'/XML',str(xml),'/F'],check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        print(json.dumps({'task':name,'installed':args.install,
                          'local_only':not args.enable_delivery,
                          'delivery_opt_in':args.enable_delivery,
                          'definition':str(xml)}))


if __name__=='__main__': main()
