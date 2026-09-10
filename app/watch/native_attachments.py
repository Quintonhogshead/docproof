"""Freeze every received attachment slot before any native book edit."""
from __future__ import annotations

import shutil
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree

from docproof.interior.workflow import digest
from . import native_files
from .native_queue import QueueError


def validate(path):
    path = Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise QueueError('A correction attachment is empty or missing.')
    suffix = path.suffix.casefold()
    try:
        if suffix == '.docx':
            with ZipFile(path) as archive:
                ElementTree.fromstring(archive.read('word/document.xml'))
                if archive.testzip():
                    raise ValueError('Damaged Word file')
        elif suffix == '.pdf':
            from pypdf import PdfReader
            if not len(PdfReader(path).pages):
                raise ValueError('Empty PDF')
        elif suffix in {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp', '.bmp'}:
            from PIL import Image
            with Image.open(path) as picture:
                picture.verify()
        elif suffix in {'.txt', '.md'}:
            if not path.read_text('utf-8-sig').strip():
                raise ValueError('Empty text file')
        else:
            raise ValueError('Unsupported file format')
    except Exception as exc:
        raise QueueError(f'Attachment {path.name} could not be read as a supported correction file. Supply a readable PDF, DOCX, image or text file.') from exc


def gather(job, job_dir, urls, token, home, *, opener, save):
    paths = dict(job.get('submission_paths_by_index') or {})
    receipts = dict(job.get('attachment_receipts') or {})
    unique, missing, seen = [], [], {}
    root = (Path(job_dir) / 'attachments').resolve()
    job.update(expected_attachment_count=len(urls), received_attachment_count=len(receipts))
    save()
    for index, url in enumerate(urls):
        slot = str(index)
        prior = receipts.get(slot)
        path = Path(paths[slot]).resolve() if slot in paths else None
        if path:
            if not path.is_relative_to(root):
                raise QueueError('A frozen attachment path left its batch folder.')
            if not prior or not path.is_file() or digest(path) != prior.get('sha256') or prior.get('url') != url:
                raise QueueError('A frozen correction attachment changed or disappeared. The batch requires recovery.')
        else:
            destination = root / str(index + 1)
            cached = native_files.cached_file(url, Path(home) / 'manual-attachments')
            if cached:
                destination.mkdir(parents=True, exist_ok=True)
                path = destination / Path(cached).name
                shutil.copy2(cached, path)
            else:
                try:
                    path = native_files.download_file(token, url, destination, opener=opener,
                                                       fallback_name=f'submission-{index + 1}')
                except native_files.ManualAttachmentRequired as exc:
                    missing.append({'slot': slot, 'file_id': str(exc.file_id or ''),
                                    'filename': str(exc.filename or f'submission-{index + 1}'), 'url': url})
                    continue
            path = Path(path).resolve()
            if not path.is_relative_to(root):
                raise QueueError('The attachment download left its batch folder.')
        validate(path)
        sha = digest(path)
        receipt = {'slot': slot, 'url': url, 'file_id': native_files.file_id(url),
                   'filename': path.name, 'sha256': sha, 'bytes': path.stat().st_size,
                   'duplicate_of': seen.get(sha)}
        receipts[slot], paths[slot] = receipt, str(path)
        if sha not in seen:
            unique.append(path)
            seen[sha] = slot
        job.update(attachment_receipts=receipts, submission_paths_by_index=paths,
                   expected_attachment_count=len(urls), received_attachment_count=len(receipts))
        save()
    if not missing and set(receipts) != {str(i) for i in range(len(urls))}:
        raise QueueError('The received attachment manifest does not cover every submitted file.')
    return unique, missing
