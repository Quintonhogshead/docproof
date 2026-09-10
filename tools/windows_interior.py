"""Local Windows controls and an opt-in real InDesign/Astra rehearsal."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def configure(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    from docproof.platform_io import private_path
    private_path(home, 0o700)
    os.environ['GALLEY_CODEX_HOME'] = str(home / 'codex')
    os.environ['DOCPROOF_HOME'] = str(home)
    # This dedicated desktop uses its explicitly chosen Credential Manager
    # connection, even if a shell inherited a token for another HubSpot app.
    os.environ.pop('HUBSPOT_TOKEN', None)
    if not os.environ.get('GALLEY_CODEX_BIN'):
        candidates=sorted((Path.home()/'AppData/Local/OpenAI/Codex/bin').glob('*/codex.exe'),
                          key=lambda p:p.stat().st_mtime,reverse=True)
        if candidates: os.environ['GALLEY_CODEX_BIN']=str(candidates[0])
    import shutil
    if not shutil.which('pdftoppm'):
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin'
        if (bundled/'pdftoppm.exe').is_file():
            os.environ['PATH']=str(bundled)+os.pathsep+os.environ.get('PATH','')


def rehearsal(home: Path, *, astra=False):
    from docproof.interior.native import InDesignWorker
    from docproof.interior.workflow import digest, run_local, save_json
    from docproof.interior.verify import prepare_edits, check_saved
    from galley.codex_runner import check_login
    if astra:
        check_login()
    work = home / 'rehearsals' / uuid.uuid4().hex
    work.mkdir(parents=True)
    source = work / 'Windows Test - Book 1.indd'
    script = work / 'create-test.jsx'
    # A disposable document, always closed; user documents are never selected.
    script.write_text('''var d=null; try {
      var out=new File(%s); if(out.exists) throw Error("test source already exists");
      d=app.documents.add(false);
      d.documentPreferences.pageWidth="6in"; d.documentPreferences.pageHeight="9in";
      d.documentPreferences.facingPages=false;
      var f=d.pages[0].textFrames.add(); f.geometricBounds=[36,36,500,390];
      f.contents="Windows correction rehearsal\\rThis sentnce describes a quiet river.\\rThe original edition must remain unchanged.";
      f.parentStory.texts[0].appliedFont=app.fonts.itemByName("Arial\\tRegular");
      f.parentStory.texts[0].pointSize=12;
      d.save(out); "OK";
    } finally { if(d!==null) d.close(SaveOptions.NO); }
    ''' % json.dumps(str(source), ensure_ascii=True), encoding='ascii')
    from docproof.platform_io import run_bounded
    r = run_bounded([sys.executable, '-m', 'docproof.interior.com_runner', str(script)],
                       capture_output=True, encoding='utf-8', timeout=90,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode or not source.is_file():
        raise RuntimeError('InDesign could not create the rehearsal document: ' + r.stderr[-800:])
    original_hash = digest(source)
    if astra:
        result = run_local(source, [], 'Correct sentnce to sentence. Set only the words quiet river in italic. Make no other changes.', work / 'astra-job')
        if result['status'] != 'verified':
            save_json(work / 'rehearsal-result.json', result)
            raise RuntimeError('The Astra rehearsal did not verify. Inspect ' + str(work))
    else:
        worker = InDesignWorker(timeout=90)
        baseline = worker.inspect(source, work / 'native-job')
        story = next(s for s in baseline['stories'] if 'sentnce' in s['text'])
        raw = [{'id':'typo', 'story_id':story['id'], 'find':'sentnce', 'replacement':'sentence', 'expected_count':1},
               {'id':'style', 'story_id':story['id'], 'find':'quiet river', 'replacement':'quiet river', 'expected_count':1, 'font_style':'Italic'}]
        prepare_edits(baseline, raw)
        edits = raw
        output = work / 'Windows Test - Book 2.indd'
        worker.apply(source, output, edits, work / 'native-job')
        final = worker.verify(output, work / 'reopened')
        verification = check_saved(baseline, final, edits)
        if not verification['passed']:
            raise RuntimeError('The saved rehearsal failed verification: ' + '; '.join(verification['failures']))
        result = {'status':'verified', 'output_indd':str(output), 'output_pdf':final['output_pdf']}
    if digest(source) != original_hash:
        raise RuntimeError('The rehearsal source changed.')
    save_json(work / 'rehearsal-result.json', {**result, 'astra':astra, 'source_preserved':True})
    save_json(home / ('astra-rehearsal.json' if astra else 'native-rehearsal.json'),
              {'status':'verified', 'work_dir':str(work), 'source_preserved':True})
    print(json.dumps({'status':'verified', 'astra':astra, 'work_dir':str(work)}), flush=True)


def _poll_args(home: Path, *, enable_delivery: bool = False) -> list[str]:
    """Build the native poll command without changing saved settings."""
    args = ['poll', '--watch-home', str(home / 'watch'), '--continuous', '--interval', '300']
    if not enable_delivery:
        args.append('--local-only')
        return args
    from app.watch.settings import WatchSettings
    ws = WatchSettings.load(home / 'watch')
    if not ws.corrections_native_auto_upload or not ws.corrections_native_form_poll:
        raise SystemExit(
            'Delivery opt-in requires saved corrections_native_auto_upload and '
            'corrections_native_form_poll settings; no settings were changed.')
    return args


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['native-test','astra-test','login','ui','poll'])
    parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--enable-delivery', action='store_true',
                        help='Opt into delivery only when saved native settings allow it')
    args=parser.parse_args()
    if args.enable_delivery and args.action != 'poll':
        parser.error('--enable-delivery is valid only for poll')
    home=args.home.resolve()
    configure(home)
    if args.action in ('native-test','astra-test'):
        from app.lock import FolderLock
        with FolderLock(home/'rehearsal-owner'):
            rehearsal(home, astra=args.action=='astra-test')
    elif args.action=='login':
        from galley.codex_runner import codex_home, _private_dir, child_env, _binary, _AUTH_OPTIONS
        cache=_private_dir(codex_home())
        raise SystemExit(subprocess.call([_binary(None),*_AUTH_OPTIONS,'login'],env=child_env(cache),cwd=cache))
    elif args.action=='ui':
        from app.main import create_app
        import uvicorn
        from docproof.interior.desktop_setup import add_setup_routes
        # This panel never starts unrelated proofing/formatting/marketing clocks.
        app=create_app(home,start_runner=False)
        add_setup_routes(app,home,Path(__file__).resolve())
        uvicorn.run(app,host='127.0.0.1',port=args.port)
    elif args.action=='poll':
        from docproof.interior.__main__ import main as interior_main
        raise SystemExit(interior_main(_poll_args(home, enable_delivery=args.enable_delivery)))


if __name__=='__main__':
    main()
