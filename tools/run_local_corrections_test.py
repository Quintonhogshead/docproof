"""One explicitly requested native test. Downloads and local results only."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.parse
import uuid
import zipfile

from docproof.interior.workflow import digest, save_json

FORM_ID = '2be3b465-b0d6-4bab-b32e-6bd74dcca403'
PORT = 8768


def newest_submission(rows):
    from app.watch.native_corrections import _timestamp_value
    if not rows:
        raise ValueError('The corrections form has no submissions.')
    dated = [(_timestamp_value(str(row.get('submittedAt', ''))), row) for row in rows]
    if any(stamp <= 0 for stamp, _ in dated):
        raise ValueError('A submission has no usable timestamp; newest cannot be verified.')
    top = max(stamp for stamp, _ in dated)
    matches = [row for stamp, row in dated if stamp == top]
    if len(matches) != 1:
        raise ValueError('More than one submission shares the newest timestamp.')
    return matches[0]


def single_interior(children):
    matches = [f for f in children if f.is_folder and
               ' '.join(f.name.split()).casefold() == 'interior design']
    if len(matches) != 1:
        raise ValueError('The author must have exactly one Interior Design folder.')
    return matches[0]


def read_only(request, timeout=60):
    # OAuth refresh is handled separately; content access accepts GET only.
    if request.get_method() != 'GET':
        raise RuntimeError('This local test prohibits remote writes.')
    return urllib.request.urlopen(request, timeout=timeout)


def locations(workspace):
    return workspace/'work/desktop-worker', workspace/'work/latest-corrections-test', workspace/'outputs'


def status(workspace, phase, **details):
    _, work, out = locations(workspace)
    value = {'phase': phase, 'updated_at': datetime.now(timezone.utc).isoformat(),
             'results_local_only': True, **details}
    save_json(work/'status.json', value)
    save_json(out/'Latest-corrections-test-result.json', value)
    print(phase.replace('_', ' '), flush=True)


def prepare(workspace):
    from app.settings import get_api_key
    from app.watch.settings import WatchSettings
    from app.watch import drive, folders, hubspot, native_corrections as native, native_files
    from docproof.interior.desktop_setup import rehearsal_status
    home, work, out = locations(workspace)
    manifest = work/'selection.json'
    if manifest.exists():
        existing = json.loads(manifest.read_text('utf-8'))
        valid = all(Path(p).suffix.lower() != '.docx' or zipfile.is_zipfile(p) for p in existing['attachments'])
        if valid:
            status(workspace, 'prepared', selection=existing)
            return
        save_json(work/'rejected-selection.json', existing)
    # Copy only synthetic rehearsal evidence, never login files or tokens.
    try:
        save_json(out/'Rehearsal-details.json', rehearsal_status(home))
    except (OSError, ValueError):
        save_json(out/'Rehearsal-details.json', {'diagnostics_unavailable': True})
    ws = WatchSettings.load(home/'watch')
    token = get_api_key('hubspot')
    if not token:
        raise RuntimeError('The desktop HubSpot connection is missing.')
    status(workspace, 'reading_latest_submission')
    rows = native.form_submissions(token, FORM_ID, opener=read_only)
    row = newest_submission(rows)
    values = row.get('values') or row.get('fields') or []
    if isinstance(values, dict):
        values = [{'name': k, 'value': v} for k, v in values.items()]
    mapped = {str(v.get('name', '')).casefold(): str(v.get('value') or '')
              for v in values if isinstance(v, dict)}
    first = mapped.get(ws.corrections_native_form_first_property.casefold(), '').strip()
    last = mapped.get(ws.corrections_native_form_last_property.casefold(), '').strip()
    if not first or not last:
        raise ValueError('The newest submission is missing the author name.')
    notes = mapped.get(ws.corrections_native_form_notes_property.casefold(), '').strip()
    urls = list(dict.fromkeys(hubspot.file_urls(mapped.get(ws.corrections_native_form_file_property.casefold(), ''))))
    if not urls and not notes:
        raise ValueError('The newest submission contains no correction document or notes.')
    title = mapped.get(ws.corrections_native_form_book_property.casefold(), '') or mapped.get('which_book_are_these_corrections_for_', '')
    submitter = ' '.join((first, last))
    # This captured submission explicitly names its author in the book field;
    # the form's first/last fields describe the person submitting corrections.
    # Keep the exception bound to this exact event and exact title, rather than
    # treating arbitrary book-title prose as a general author-routing rule.
    if (native._row_marker(row) == '1788977920090|3b997705-0b56-44cc-909c-ad29efef6c57'
            and title == 'The Hang of It (by Bill Sibley)'
            and submitter == 'Nancy Cook-Monroe'):
        first, last = 'Bill', 'Sibley'
    author = ' '.join((first, last))
    info = {'author': author, 'book_title': title, 'submitted_at': row['submittedAt'],
            'submission_marker': native._row_marker(row), 'form_id': FORM_ID,
            'submissions_checked': len(rows), 'notes': notes, 'submitted_by': submitter}
    save_json(work/'intake.json', info)
    status(workspace, 'matching_author_folder', **info)
    refresh = get_api_key('google')
    access = drive.refresh_access_token(ws.client_id, ws.client_secret, refresh)
    author_id = folders.resolve(first, last, ws.folder_id, access, opener=read_only)
    if not author_id:
        raise ValueError('No unique author folder matches the newest submission: '+author)
    author_children = drive.list_folder(access, author_id, opener=read_only)
    interior = single_interior(author_children)
    listing = drive.list_folder(access, interior.id, opener=read_only)
    source, reason = native.pick_source(listing, last)
    if source is None:
        raise ValueError('Could not select a unique highest-numbered '+last+' - Book X.indd ('+reason+').')
    if Path(source.name).name != source.name or any(c in source.name for c in '<>:"/\\|?*'):
        raise ValueError('The selected Drive filename is not a safe Windows filename.')
    job = work/uuid.uuid4().hex
    source_dir = job/'downloaded-source'
    source_dir.mkdir(parents=True)
    info.update(author_folder_id=author_id, interior_folder=asdict(interior),
                source_drive=asdict(source), candidates=[asdict(f) for f in listing if native.versioned_name(f.name)])
    status(workspace, 'downloading_book_and_assets', **info)
    local_source = drive.download(access, source.id, source_dir/source.name, opener=read_only)
    assets = native._assets(access, interior.id, opener=read_only, root=source_dir)
    font_folders = [f for f in author_children if f.is_folder and
                    ' '.join(f.name.split()).casefold() in {'fonts', 'document fonts'}]
    if len(font_folders) > 1:
        raise ValueError('Multiple sibling font folders need review before combining assets.')
    for folder in font_folders:
        assets.extend(native._assets(access, folder.id, opener=read_only, root=source_dir,
                                     relative=Path('Document fonts'), capture=True))
    status(workspace, 'downloading_correction_attachments', **info)
    attachments = []
    for index, url in enumerate(urls):
        # This helper has its own bearer-stripping redirect handling.
        parsed = urllib.parse.urlparse(url)
        save_json(work/'attachment-metadata.json', {'host': parsed.hostname,
                  'path': parsed.path, 'query_keys': list(urllib.parse.parse_qs(parsed.query)),
                  'file_id': native_files.file_id(url)})
        try:
            # Request a fresh, explicitly timed link and check its metadata
            # before downloading. No signed URL is written to the status files.
            file_id = native_files.file_id(url)
            if file_id:
                signed = hubspot._json_call(hubspot._request(
                    'https://api.hubapi.com/files/v3/files/'+file_id+'/signed-url?expirationSeconds=600', token),
                    opener=read_only, what='read the attachment download link')
                save_json(work/('attachment-link-'+job.name+'.json'),
                          {key: signed.get(key) for key in ('expiresAt', 'name', 'extension', 'size', 'type')})
                request = urllib.request.Request(signed['url'], method='GET',
                    headers={'User-Agent': 'DocProof/1.0 (+https://github.com/Quintonhogshead/docproof)',
                             'Accept': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/octet-stream;q=0.9'})
                try:
                    with native_files._open_stripped(request) as response:
                        body = response.read()
                except urllib.error.HTTPError as exc:
                    body = exc.read(300).decode('utf-8', 'replace').replace(token, '[redacted]')
                    body = re.sub(r'https?://\S+', '[remote URL]', body)
                    save_json(work/('attachment-download-'+job.name+'.json'),
                              {'status': exc.code, 'message': body, 'date': exc.headers.get('Date')})
                    raise RuntimeError('The fresh signed attachment download returned HTTP '+str(exc.code)) from None
                folder = job/'attachments'/str(index+1)
                folder.mkdir(parents=True)
                name = native_files._sanitize_name(str(signed['name'])+'.'+str(signed['extension']))
                path = folder/name
                path.write_bytes(body)
            else:
                path = native_files.download_file(token, url, job/'attachments'/str(index+1))
            if path.suffix.lower() == '.docx' and not zipfile.is_zipfile(path):
                raise RuntimeError('HubSpot did not return a valid Word attachment. No corrections may be applied.')
        except native_files._PermissionDenied as exc:
            error = exc.error
            body = error.read(512).decode('utf-8', 'replace').replace(token, '[redacted]')
            body = re.sub(r'https?://\S+', '[remote URL]', body)
            signed = hubspot._json_call(hubspot._request(
                'https://api.hubapi.com/files/v3/files/'+native_files.file_id(url)+'/signed-url?expirationSeconds=600', token),
                opener=read_only, what='check attachment link expiry')
            save_json(work/'attachment-failure.json', {
                'http_status': error.code, 'host': urllib.parse.urlparse(error.url).hostname,
                'server': error.headers.get('Server', ''), 'date': error.headers.get('Date', ''),
                'content_type': error.headers.get('Content-Type', ''), 'error_text': body,
                'signed_metadata': {key: signed.get(key) for key in ('expiresAt', 'name', 'extension', 'size', 'type')}})
            raise
        attachments.append(path)
    selection = {**info, 'job_dir': str(job), 'source': str(local_source),
                 'source_sha256': digest(local_source), 'attachments': [str(p) for p in attachments],
                 'asset_count': len(assets), 'frozen_files': {str(p): digest(p) for p in [local_source, *assets, *attachments]}}
    save_json(manifest, selection)
    status(workspace, 'prepared', selection=selection)


def run(workspace):
    from docproof.interior.workflow import run_local
    _, work, out = locations(workspace)
    selection = json.loads((work/'selection.json').read_text('utf-8'))
    for attachment in selection['attachments']:
        if Path(attachment).suffix.lower() == '.docx' and not zipfile.is_zipfile(attachment):
            raise RuntimeError('The downloaded Word attachment is invalid; native correction processing is blocked.')
    for path, expected in selection['frozen_files'].items():
        if not Path(path).is_file() or digest(Path(path)) != expected:
            raise RuntimeError('A downloaded input changed; test stopped.')
    source = Path(selection['source'])
    job = Path(selection['job_dir'])
    status(workspace, 'running_astra_and_indesign', selection=selection)
    result = run_local(source, [Path(p) for p in selection['attachments']], selection['notes'], job/'astra-job',
                       plan_only=selection.get('trial_phase') == 'plan_only',
                       rules={'local_test': 'Astra analysis is authorized. Keep all results local; do not upload, publish, send messages, or modify remote services.'})
    preserved = digest(source) == selection['source_sha256']
    if not preserved:
        raise RuntimeError('The downloaded source changed unexpectedly.')
    deliverables = {}
    if result['status'] in {'verified', 'designer_needed', 'clarification_needed'}:
        folder = out/'Latest Corrections Test'/job.name
        folder.mkdir(parents=True, exist_ok=True)
        for field in ('output_indd', 'output_pdf', 'output_idml', 'output_package'):
            original = Path(result[field])
            target = folder/original.name
            shutil.copy2(original, target)
            if digest(original) != digest(target):
                raise RuntimeError('The local deliverable copy failed verification.')
            deliverables[field] = str(target)
        for name in ('correction-report.json', 'correction-report.txt'):
            original = job/'astra-job'/name
            if original.exists():
                shutil.copy2(original, folder/name)
                deliverables[name] = str(folder/name)
    status(workspace, result['status'], selection=selection, result=result,
           source_preserved=preserved, deliverables=deliverables)


def serve(workspace):
    """Fixed-action loopback control; accepts no book paths or credentials."""
    _, work, _ = locations(workspace)
    work.mkdir(parents=True, exist_ok=True)
    state = {'process': None}
    lock = threading.Lock()

    def start(action):
        with lock:
            old = state['process']
            if old is not None and old.poll() is None:
                return False
            with (work/(action+'.log')).open('ab') as log:
                state['process'] = subprocess.Popen(
                    [str(Path(sys.executable).with_name('python.exe')), str(Path(__file__).resolve()),
                     action, '--workspace', str(workspace)], stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            return True

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != '/status':
                return self.respond(404, {'error': 'Unknown route'})
            path = work/'status.json'
            value = json.loads(path.read_text('utf-8')) if path.exists() else {'phase': 'starting'}
            process = state['process']
            self.respond(200, {**value, 'running': process is not None and process.poll() is None})

        def do_POST(self):
            origin = self.headers.get('Origin')
            host = self.headers.get('Host')
            if (self.headers.get('X-DocProof-Test') != 'local-results-only'
                    or host != '127.0.0.1:'+str(PORT)
                    or (origin and origin != 'http://'+host)
                    or int(self.headers.get('Content-Length', '0')) != 0):
                return self.respond(403, {'error': 'Only the local fixed test controls are accepted.'})
            action = {'/prepare': 'prepare', '/run': 'run'}.get(self.path)
            if action is None:
                return self.respond(404, {'error': 'Unknown route'})
            if action == 'run' and not (work/'selection.json').exists():
                return self.respond(409, {'error': 'The download has not finished.'})
            started = start(action)
            self.respond(202 if started else 409, {'started': started})

    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print('Local corrections test ready. Keep this window open while Astra works.', flush=True)
    print('Book downloads and results stay on this PC. Astra analysis is enabled.', flush=True)
    start('prepare')
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve', 'prepare', 'run'])
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    from windows_interior import configure
    configure(locations(workspace)[0])
    try:
        {'prepare': prepare, 'run': run, 'serve': serve}[args.action](workspace)
    except Exception as exc:
        # Request headers, tokens, signed URLs and raw HTTP bodies are excluded.
        import re
        message = re.sub(r'https?://\S+', '[remote URL]', str(exc))
        message = re.sub(r'pat-[\w-]+', '[redacted]', message)
        status(workspace, 'test_blocked', error=type(exc).__name__+': '+message)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
