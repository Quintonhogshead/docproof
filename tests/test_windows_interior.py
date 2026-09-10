"""Windows transport, isolation and cancellation contracts."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from docproof import platform_io
from docproof.interior.native import InDesignWorker


def _load_tool(filename, name):
    source = Path(__file__).resolve().parents[1] / 'tools' / filename
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != 'nt', reason='Windows integration primitives')
def test_private_directory_excludes_other_accounts(tmp_path):
    import win32security
    folder=tmp_path/'private'
    folder.mkdir()
    platform_io.private_path(folder,0o700)
    info=win32security.GetNamedSecurityInfo(str(folder),win32security.SE_FILE_OBJECT,
                                           win32security.DACL_SECURITY_INFORMATION)
    acl=info.GetSecurityDescriptorDacl()
    assert acl.GetAceCount()==2
    assert info.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED


@pytest.mark.skipif(os.name != 'nt', reason='Windows process containment')
def test_model_timeout_terminates_its_windows_process(tmp_path):
    from galley.codex_runner import _execute
    result=_execute([getattr(sys,'_base_executable',sys.executable),'-c','import time; time.sleep(30)'],
                    prompt=None,env=dict(os.environ),cwd=tmp_path,timeout=0.25)
    assert result['timed_out'] is True
    assert result['returncode'] != 0


@pytest.mark.skipif(os.name != 'nt', reason='Windows process containment')
def test_model_is_stopped_when_containment_setup_fails(tmp_path, monkeypatch):
    import win32job
    from galley.codex_runner import _execute
    children=[]
    original_spawn=subprocess.Popen
    def spawn(*args,**kwargs):
        proc=original_spawn(*args,**kwargs)
        children.append(proc)
        return proc
    def unavailable(*args):
        raise OSError('Job objects unavailable')
    monkeypatch.setattr(win32job,'CreateJobObject',unavailable)
    monkeypatch.setattr(subprocess,'Popen',spawn)
    with pytest.raises(OSError,match='Job objects unavailable'):
        _execute([getattr(sys,'_base_executable',sys.executable),'-c',
                  'import time; time.sleep(30)'],
                 prompt=None,env=dict(os.environ),cwd=tmp_path,timeout=10)
    # The helper waits for the actual child to exit before returning the error.
    assert len(children)==1
    assert children[0].poll() is not None


@pytest.mark.skipif(os.name != 'nt', reason='Windows helper process containment')
def test_native_timeout_stops_the_venv_helper(tmp_path):
    # sys.executable is deliberately the venv redirector used by the worker.
    with pytest.raises(subprocess.TimeoutExpired) as error:
        platform_io.run_bounded([sys.executable,'-c','import time; print("started",flush=True); time.sleep(30)'],
                                capture_output=True,encoding='utf-8',timeout=0.5,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    # communicate would hang if an orphaned child retained the output pipe.
    assert 'started' in error.value.output


def test_startup_task_uses_interactive_desktop_and_ignores_overlap(tmp_path):
    import xml.etree.ElementTree as ET
    module = _load_tool('install_native_interior_windows.py', 'windows_startup')
    home=tmp_path/'worker & review'
    root=ET.fromstring(module.task_xml(home/'pythonw.exe',home/'launch.py','poll',home,'S-1-5-21-123'))
    ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert root.find('.//t:LogonType',ns).text=='InteractiveToken'
    assert root.find('.//t:RunLevel',ns).text=='LeastPrivilege'
    assert root.find('.//t:MultipleInstancesPolicy',ns).text=='IgnoreNew'
    assert root.find('.//t:ExecutionTimeLimit',ns).text=='PT0S'
    assert root.find('.//t:Arguments',ns).text==subprocess.list2cmdline([str(home/'launch.py'),'poll'])
    assert root.find('.//t:Command',ns).text==str(home/'pythonw.exe')


def test_windows_poll_defaults_to_local_only(tmp_path):
    module = _load_tool('windows_interior.py', 'windows_interior_args')
    assert module._poll_args(tmp_path) == [
        'poll', '--watch-home', str(tmp_path / 'watch'), '--continuous',
        '--interval', '300', '--local-only']


def test_windows_poll_delivery_requires_saved_settings_and_does_not_change_them(tmp_path):
    module = _load_tool('windows_interior.py', 'windows_interior_delivery')
    from app.watch.settings import WatchSettings

    with pytest.raises(SystemExit, match='saved corrections_native_auto_upload'):
        module._poll_args(tmp_path, enable_delivery=True)

    watch = tmp_path / 'watch'
    WatchSettings(corrections_native_auto_upload=True,
                  corrections_native_form_poll=True).save(watch)
    args = module._poll_args(tmp_path, enable_delivery=True)
    assert '--local-only' not in args
    saved = WatchSettings.load(watch)
    assert saved.corrections_native_auto_upload is True
    assert saved.corrections_native_form_poll is True


def test_installed_launcher_bakes_delivery_opt_in_without_shell_arguments(tmp_path):
    module = _load_tool('install_native_interior_windows.py', 'windows_installer_source')
    source = module.launcher_source(
        tmp_path / 'worker & review', tmp_path / 'windows_interior.py', 8767,
        r'C:\Tools & Runtime', r'C:\Codex\codex.exe', enable_delivery=True)
    compile(source, '<generated-launcher>', 'exec')
    assert "['--enable-delivery'] if action == 'poll'" in source
    assert "runpy.run_path(sys.argv[0],run_name='__main__')" in source


def test_installer_stages_local_only_by_default_and_delivery_only_when_opted_in(tmp_path, monkeypatch):
    module = _load_tool('install_native_interior_windows.py', 'windows_installer_staging')
    import types

    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'pythonw.exe').write_bytes(b'fake')
    token = types.SimpleNamespace(Close=lambda: None)
    security = types.SimpleNamespace(
        TOKEN_QUERY=1, TokenUser=2,
        OpenProcessToken=lambda *args: token,
        GetTokenInformation=lambda *args: (b'user',),
        ConvertSidToStringSid=lambda value: 'S-1-5-21-test',
    )
    api = types.SimpleNamespace(GetCurrentProcess=lambda: object())
    monkeypatch.setitem(sys.modules, 'win32api', api)
    monkeypatch.setitem(sys.modules, 'win32security', security)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    monkeypatch.setattr(module.sys, 'executable', str(runtime / 'python.exe'))
    monkeypatch.setattr(module.sys, 'argv', ['installer', '--home', str(tmp_path / 'local')])
    module.main()
    local_launcher = (tmp_path / 'local' / 'startup' / 'launch.py').read_text()
    assert "action == 'poll' and False" in local_launcher
    assert f"repo_root=Path({str(Path(__file__).resolve().parents[1])!r})" in local_launcher
    assert (tmp_path / 'local' / 'startup' / 'poll.xml').exists()

    from app.watch.settings import WatchSettings
    delivery_home = tmp_path / 'delivery'
    WatchSettings(corrections_native_auto_upload=True,
                  corrections_native_form_poll=True).save(delivery_home / 'watch')
    monkeypatch.setattr(module.sys, 'argv', [
        'installer', '--home', str(delivery_home), '--enable-delivery'])
    module.main()
    delivery_launcher = (delivery_home / 'startup' / 'launch.py').read_text()
    assert "['--enable-delivery'] if action == 'poll'" in delivery_launcher
    assert "sys.path.insert(0,str(repo_root))" in delivery_launcher
    assert "os.environ['PYTHONPATH']=str(repo_root)" in delivery_launcher


def test_generated_launcher_pins_parent_and_child_imports_to_checkout(tmp_path):
    module = _load_tool('install_native_interior_windows.py', 'windows_launcher_imports')
    selected = tmp_path / 'selected'
    (selected / 'tools').mkdir(parents=True)
    old = tmp_path / 'old'
    old.mkdir()
    (selected / 'originmod.py').write_text("ORIGIN='selected'\n", encoding='utf-8')
    (old / 'originmod.py').write_text("ORIGIN='old'\n", encoding='utf-8')
    result = tmp_path / 'origins.json'
    tool = selected / 'tools' / 'fake_worker.py'
    tool.write_text(
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "import originmod\n"
        "child = subprocess.check_output([sys.executable, '-c', "
        "'import originmod; print(originmod.ORIGIN)'], text=True, timeout=10).strip()\n"
        "Path(os.environ['ORIGINS']).write_text(json.dumps({'parent': originmod.ORIGIN, 'child': child}))\n",
        encoding='utf-8')
    launcher = tmp_path / 'launch.py'
    (tmp_path / 'home').mkdir()
    launcher.write_text(module.launcher_source(
        tmp_path / 'home', tool, 8767, '', '', enable_delivery=False), encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(old), ORIGINS=str(result))
    subprocess.run([sys.executable, str(launcher), 'ui'], env=env, check=True,
                   capture_output=True, text=True, timeout=30)
    assert json.loads(result.read_text(encoding='utf-8')) == {
        'parent': 'selected', 'child': 'selected'}


def test_review_manifest_includes_instructions_without_edits():
    from docproof.interior.astra import _review_manifest
    result=_review_manifest({}, {'stories':[]},
                            {'instructions':[{'id':'typo'},{'id':'designer-request'}]}, [])
    assert result['instruction_ids']==['typo','designer-request']


def test_desktop_setup_blocks_cross_origin_test_starts_and_excludes_auth(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.staticfiles import StaticFiles
    from docproof.interior.desktop_setup import add_setup_routes
    app=FastAPI()
    app.mount('/',StaticFiles(directory=tmp_path),name='frontend')
    folder=tmp_path/'rehearsals/example/astra-job'
    folder.mkdir(parents=True)
    (folder/'plan.json').write_text('{"instructions": []}')
    (folder/'auth.json').write_text('{"token":"secret-that-must-stay-private"}')
    add_setup_routes(app,tmp_path,tmp_path/'worker.py')
    client=TestClient(app)
    assert client.post('/api/interior/rehearse').status_code==403
    assert client.post('/api/interior/rehearse',headers={'x-docproof-rehearsal':'disposable-only',
                                                       'origin':'https://unrelated.example'}).status_code==403
    result=client.get('/api/interior/setup-status')
    assert result.status_code==200
    assert 'secret-that-must-stay-private' not in result.text
    assert result.json()['rehearsals'][0]['plan.json']=={'instructions':[]}


@pytest.mark.skipif(os.name != 'nt', reason='Windows COM subprocess command')
def test_windows_indesign_uses_isolated_child_and_keeps_paths(tmp_path):
    source=tmp_path/'Book with spaces.indd'
    source.write_bytes(b'source')
    calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        work=Path(argv[-1]).parent
        (work/'native-baseline.json').write_text(json.dumps({'stories':[], 'fonts':[], 'links':[], 'overset':[]}))
        (work/'baseline.pdf').write_bytes(b'pdf')
        (work/'baseline.idml').write_bytes(b'idml')
        return subprocess.CompletedProcess(argv,0,'OK','')
    InDesignWorker(runner=run,platform='win32').inspect(source,tmp_path/'work with spaces')
    argv,kwargs=calls[0]
    assert argv[1:3]==['-m','docproof.interior.com_runner']
    assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW
    assert kwargs['timeout'] > 0
