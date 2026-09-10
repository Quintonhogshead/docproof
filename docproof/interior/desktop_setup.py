"""Loopback-only disposable rehearsal controls for the Windows review panel."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from fastapi import HTTPException, Request


def rehearsal_status(home: Path) -> dict:
    result={"disposable_controls_only":True,"rehearsals":[]}
    for folder in sorted((home/'rehearsals').glob('*'),key=lambda p:p.stat().st_mtime)[-3:]:
        item={"id":folder.name}
        for name in ('plan.json','technical-block.json','workflow.json','verification.json','rehearsal-result.json'):
            path=(folder/name) if name=='rehearsal-result.json' else folder/'astra-job'/name
            if path.is_file(): item[name]=json.loads(path.read_text(encoding='utf-8'))
        item['model_results']=[json.loads(p.read_text(encoding='utf-8'))
                               for p in folder.glob('astra-job/**/codex-requests/*/result.json')]
        result['rehearsals'].append(item)
    return result


def add_setup_routes(app, home: Path, tool: Path):
    """No caller-provided paths, commands, tokens or live books are accepted."""
    state={'process':None}
    lock=threading.Lock()

    @app.get('/api/interior/setup-status')
    def status():
        with lock:
            process=state['process']
            running=process is not None and process.poll() is None
        return {**rehearsal_status(home),'test_running':running,
                'test_exit_code':process.poll() if process is not None else None}

    @app.post('/api/interior/rehearse')
    def start(request: Request):
        # Custom header + same-origin check reject drive-by browser form posts.
        if request.headers.get('x-docproof-rehearsal')!='disposable-only':
            raise HTTPException(403,'This endpoint only runs a disposable rehearsal.')
        origin=request.headers.get('origin')
        if origin and origin!=str(request.base_url).rstrip('/'):
            raise HTTPException(403,'Use the local review panel.')
        with lock:
            process=state['process']
            if process is not None and process.poll() is None:
                raise HTTPException(409,'A rehearsal is already running.')
            interpreter=Path(sys.executable).with_name('python.exe')
            with (home/'rehearsal.log').open('ab') as log:
                state['process']=subprocess.Popen(
                    [str(interpreter),str(tool),'astra-test','--home',str(home)],
                    stdout=log,stderr=log,env=dict(os.environ,PYTHONIOENCODING='utf-8'),
                    creationflags=subprocess.CREATE_NO_WINDOW)
        return {'started':True,'disposable_only':True}

    # create_app already mounts the static frontend at '/'. New API routes
    # must precede that catch-all mount or the frontend returns a false 404.
    from starlette.routing import Mount
    added=app.router.routes[-2:]
    del app.router.routes[-2:]
    index=next((i for i,route in enumerate(app.router.routes)
                if isinstance(route,Mount) and route.path in {'','/'}),len(app.router.routes))
    app.router.routes[index:index]=added
