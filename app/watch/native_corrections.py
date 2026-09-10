"""Native InDesign corrections for the watcher's HubSpot/Drive stage.

This adapter deliberately has its own job ledger.  Native packages can contain
large, nested asset trees and are often resumed on another tick; putting that
state in the IDML ``Job`` record made it too easy to repeat a designer edit or
to publish a result for a newer export.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import urllib.parse
from datetime import datetime, timezone

from app.lock import FolderInUse, FolderLock

from . import drive, folders, hubspot, native_files
from .drive import DriveFile
from .stages import SOURCE_PROP
from docproof.interior.versions import (native_filename, native_version as validate_version,
                                         next_version, version_text)

log = logging.getLogger("docproof.app.watch.native_corrections")

NATIVE_MARKER = "docproof.native_corrections"
NATIVE_JOB_PROP = "docproof.native_job"
NATIVE_SOURCE_PROP = "docproof.native_source"
NATIVE_STATUS_PROP = "docproof.native_status"
_BOOK = re.compile(r"^(?P<surname>.+?)\s*[-\u2013\u2014]\s*Book\s*(?P<number>\d+(?:\.5)?)\s*\.indd$", re.I)


class NativeCorrectionError(RuntimeError):
    """A native correction could not be completed."""


@dataclass(frozen=True)
class NativeSource:
    file: DriveFile
    version: int | float
    surname: str


def versioned_name(name: str) -> tuple[str, int | float] | None:
    """Return the surname and constrained Book version from a native filename."""
    match = _BOOK.match(Path(name).name.strip())
    if not match:
        return None
    try:
        return match.group("surname").strip(), validate_version(float(match.group("number")))
    except ValueError:
        return None


def pick_source(listing: list[DriveFile], surname: str) -> tuple[DriveFile | None, str]:
    """Choose the highest numeric ``Book N.indd`` export.

    ``tie`` is intentional: two equal versions usually mean a designer copied
    an export, and silently picking by Drive order would make the result
    impossible to audit.
    """
    wanted = (surname or "").strip().casefold()
    candidates: list[tuple[int | float, DriveFile]] = []
    for entry in listing:
        parsed = versioned_name(entry.name)
        if entry.is_folder or parsed is None or parsed[0].casefold() != wanted:
            continue
        candidates.append((parsed[1], entry))
    if not candidates:
        return None, "none"
    top = max(version for version, _ in candidates)
    hits = [entry for version, entry in candidates if version == top]
    if len(hits) != 1:
        return None, "tie"
    return hits[0], ""


def newer_export(listing: list[DriveFile], surname: str, version: int | float) -> DriveFile | None:
    """Return the highest export beyond the one result version can follow."""
    candidates = [entry for entry in listing
                  if (parsed := versioned_name(entry.name))
                  and parsed[0].casefold() == surname.casefold()
                  and parsed[1] > next_version(version)]
    return max(candidates, key=lambda entry: versioned_name(entry.name)[1], default=None)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(file: DriveFile, local: Path | None = None) -> str:
    """A stable source fingerprint using Drive identity and content where able."""
    checksum = getattr(file, "md5_checksum", "") or getattr(file, "checksum", "")
    if local and local.is_file():
        checksum = _hash(local)
    return "|".join((file.id, file.modified_time or "", str(file.size or 0), checksum))


def _safe(value: str) -> str:
    return value.replace("/", "-").replace("\\", "-").replace(":", "-").strip() or "file"


def _job_dir(home: Path, job_id: str) -> Path:
    return home / "native_jobs" / job_id


def _write_json(path: Path, value: dict) -> None:
    from docproof.interior.workflow import save_json
    save_json(path, value)


def _read_jobs(home: Path) -> list[dict]:
    root = home / "native_jobs"
    if not root.is_dir():
        return []
    jobs = []
    for path in sorted(root.glob("*/job.json")):
        try:
            payload = json.loads(path.read_text("utf-8"))
            if not isinstance(payload, dict) or not payload.get('job_id'):
                raise ValueError('Invalid native job receipt')
            jobs.append(payload)
        except (OSError, ValueError) as exc:
            raise NativeCorrectionError('A native job receipt is unreadable. Restore it before processing more submissions.') from exc
    return jobs


def _job_delivery_complete(job: dict, ws) -> bool:
    """Whether discovery can stop offering a durable native job."""
    if job.get("crm_written"):
        return True
    if not ws.corrections_native_auto_upload:
        return bool(job.get("local_complete"))
    expected = set((job.get("artifact_hashes") or {}).keys())
    uploaded = set((job.get("uploaded") or {}).keys())
    return bool(expected) and expected.issubset(uploaded) and not ws.hubspot_write_back


def _manual_files_ready(job: dict, home: Path) -> bool:
    """Check cached manual attachments without touching HubSpot or Drive."""
    missing = job.get("missing_attachments") or []
    if not missing:
        return True
    cached = getattr(native_files, "cached_file", None)
    if cached is None:
        return False
    if not isinstance(missing, list):
        return False
    urls = []
    for item in missing:
        if not isinstance(item, dict):
            return False
        url = str(item.get("url", "")).strip()
        if not url or not native_files.file_id(url):
            return False
        urls.append(url)
    try:
        cache_root = home / "manual-attachments"
        for url in urls:
            if not url or cached(url, cache_root):
                continue
            file_id = getattr(native_files, "file_id", lambda _url: None)(url)
            # Let the worker diagnose a present but invalid cache manifest as
            # a technical block. A genuinely absent cache remains awaiting
            # manual input and does not consume the queue.
            if not file_id or not (cache_root / str(file_id)).exists():
                return False
        return True
    except Exception:  # noqa: BLE001 - cache is an optional local bridge
        return False


def _submission_properties(ws, record) -> tuple[list[str], str, str]:
    props = record.properties
    names = []
    # The native submission property is a frozen submission marker, not a
    # file-upload field.  Attachments come from the explicitly configured file
    # property so a marker can never be mistaken for a URL.
    for name in (ws.hubspot_corrections_file_property,):
        if name and name not in names:
            names.append(name)
    urls: list[str] = []
    for name in names:
        urls.extend(hubspot.file_urls(props.get(name, "")))
    text = ""
    for name in (ws.hubspot_corrections_text_property,):
        if name and props.get(name):
            text = str(props.get(name) or "").strip()
            break
    marker = (str(props.get(ws.corrections_native_submission_property, "") or "").strip()
              if ws.corrections_native_submission_property else "")
    return urls, text, marker


def form_submissions(token: str, form_id: str, *, opener=hubspot._open_url,
                     limit: int = 10000) -> list[dict]:
    """Read the exact HubSpot form's submission events.

    The watcher uses these events only when no durable CRM submission marker is
    configured.  A 403 is intentionally surfaced as a HubSpot error so a
    missing ``forms`` read scope cannot silently reuse the last submission.
    """
    if not form_id:
        return []
    rows: list[dict] = []
    after = ""
    while True:
        # HubSpot's submissions endpoint accepts at most 50 per page even
        # though other CRM APIs permit 100.
        query = {"limit": str(min(limit, 50))}
        if after:
            query["after"] = after
        params = urllib.parse.urlencode(query)
        request = hubspot._request(
            f"{hubspot.API}/form-integrations/v1/submissions/forms/{form_id}?{params}",
            token)
        answer = hubspot._json_call(request, opener=opener,
                                    what=f"read submissions for form {form_id}")
        page = answer.get("results") or answer.get("submissions") or []
        rows.extend(row for row in page if isinstance(row, dict))
        after = str((answer.get("paging") or {}).get("next", {}).get("after")
                    or answer.get("after") or "")
        if not after or not page:
            return rows
        if len(rows) >= limit:
            raise NativeCorrectionError(
                f"the corrections form has more than {limit} submissions; raise the cap before running")


def _timestamp_value(value: str) -> float:
    """Normalize HubSpot ISO or millisecond submission timestamps."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        number = float(text)
        # HubSpot's form API returns submittedAt in epoch milliseconds. Keep
        # accepting seconds for settings and older captured intake manifests.
        return number / 1000.0 if number > 10_000_000_000 else number
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _event_key(event: tuple[list[str], str, str]) -> str:
    return json.dumps({"urls": event[0], "text": event[1], "marker": event[2]}, sort_keys=True)


def _row_marker(row: dict) -> str:
    stamp = str(row.get("submittedAt") or "")
    event_id = str(row.get("conversionId") or row.get("id") or "")
    return f"{stamp}|{event_id}" if stamp and event_id else stamp or event_id


def _project_name_properties(ws) -> tuple[str, str]:
    """Return the configured Projects first/last fields.

    Native form intake often reads a different object schema than the legacy
    watcher. Keep the old settings as a compatibility fallback so existing
    property mode configurations do not need to be migrated.
    """
    return (getattr(ws, "corrections_native_project_first_property", "")
            or ws.hubspot_first_property,
            getattr(ws, "corrections_native_project_last_property", "")
            or ws.hubspot_last_property)


def _book_title_key(value: str) -> str:
    """Compare titles conservatively while tolerating punctuation drift."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def _events_for(record, rows: list[dict], ws) -> list[tuple[list[str], str, str]]:
    """Match form events to a CRM record, retaining every event in order."""
    project_first, project_last = _project_name_properties(ws)
    first = str(record.properties.get(project_first, "") or "").strip().casefold()
    last = str(record.properties.get(project_last, "") or "").strip().casefold()
    matches = []
    for row in rows:
        values = row.get("values") or row.get("fields") or []
        if isinstance(values, dict):
            values = [{"name": k, "value": v} for k, v in values.items()]
        mapped = {str(item.get("name", "")).casefold(): str(item.get("value", "") or "")
                  for item in values if isinstance(item, dict)}
        associated = str(row.get("recordId") or row.get("objectId") or "") == record.id
        form_first = ws.corrections_native_form_first_property.casefold()
        form_last = ws.corrections_native_form_last_property.casefold()
        first_hit = mapped.get(form_first, "").strip().casefold() == first if first else False
        last_hit = mapped.get(form_last, "").strip().casefold() == last if last else False
        first_hit = first_hit or (mapped.get("firstname", "").strip().casefold() == first if first else False)
        last_hit = last_hit or (mapped.get("lastname", "").strip().casefold() == last if last else False)
        if ws.corrections_native_form_book_property and ws.corrections_native_project_book_property:
            book_form = _book_title_key(mapped.get(ws.corrections_native_form_book_property.casefold(), ""))
            book_project = _book_title_key(record.properties.get(ws.corrections_native_project_book_property, ""))
            # A CRM association verifies the Project identity, but it does
            # not make a contradictory book title safe to process.
            if not book_form or not book_project or book_form != book_project:
                continue
        submitted = str(row.get("submittedAt", "") or "")
        if ws.corrections_native_start_after and _timestamp_value(submitted) <= _timestamp_value(ws.corrections_native_start_after):
            continue
        if associated or (first_hit and last_hit):
            matches.append((_timestamp_value(str(row.get("submittedAt", "") or "")), row, mapped))
    events = []
    for _stamp, row, mapped in sorted(matches, key=lambda item: item[0]):
        # Form submissions are event snapshots, so only explicitly named form
        # fields are accepted. Falling back to mutable Project properties here
        # could interpret an unrelated CRM URL as a new submission.
        file_field = ws.corrections_native_form_file_property
        notes_field = ws.corrections_native_form_notes_property
        urls = hubspot.file_urls(mapped.get(file_field.casefold(), "")) if file_field else []
        text = mapped.get(notes_field.casefold(), "").strip() if notes_field else ""
        marker = _row_marker(row)
        events.append((list(dict.fromkeys(urls)), text, marker))
    return events


def _project_records(token: str, ws, *, opener) -> list[hubspot.HubSpotRecord]:
    """Read Projects for form-driven intake without using the status gate."""
    project_first, project_last = _project_name_properties(ws)
    props = [p for p in (project_first, project_last,
                         ws.corrections_native_folder_property,
                         ws.corrections_native_project_id_property,
                         ws.corrections_native_project_book_property) if p]
    records: list[hubspot.HubSpotRecord] = []
    after = ""
    seen: set[str] = set()
    seen_ids: set[str] = set()
    while True:
        if ws.corrections_native_shared_form:
            # Include incomplete Projects: their titles still make a shared
            # form ambiguous, even when the author's name is missing.
            query = urllib.parse.urlencode({"properties": ','.join(props), "limit": 100,
                                            **({"after": after} if after else {})})
            request = hubspot._request(
                f"{hubspot.API}/crm/v3/objects/{ws.hubspot_object}?{query}", token)
        else:
            body = {"filterGroups": [{"filters": [{"propertyName": project_first,
                    "operator": "HAS_PROPERTY"}]}], "properties": props, "limit": 100}
            if after:
                body['after'] = after
            request = hubspot._request(f"{hubspot.API}/crm/v3/objects/{ws.hubspot_object}/search",
                                      token, data=json.dumps(body).encode(), method='POST')
        answer = hubspot._json_call(request, opener=opener,
                                    what="find Projects for native corrections")
        for raw in answer.get('results') or []:
            if not isinstance(raw, dict) or not raw.get('id') or str(raw['id']) in seen_ids:
                raise NativeCorrectionError('HubSpot pagination returned an invalid or repeated Project; no title match is safe.')
            seen_ids.add(str(raw['id']))
            records.append(hubspot.HubSpotRecord.from_api(raw))
        if len(records) > 50000:
            raise NativeCorrectionError('HubSpot Projects exceed 50000 records; no partial title matching is allowed.')
        after = str((answer.get("paging") or {}).get("next", {}).get("after") or "")
        if not after:
            return records
        if after in seen:
            raise NativeCorrectionError("HubSpot Projects pagination is invalid or exceeds 50000 records")
        seen.add(after)


def _folder_for(token: str, ws, record, *, opener) -> str | None:
    """Resolve the explicitly mapped book folder, then legacy author folders."""
    mapped = (record.properties.get(ws.corrections_native_folder_property, "")
              if ws.corrections_native_folder_property else "").strip()
    if mapped:
        # A pasted Drive URL is accepted alongside a bare id.
        try:
            from .settings import folder_id_from
            return folder_id_from(mapped)
        except ValueError:
            return mapped
    project_first, project_last = _project_name_properties(ws)
    first = (record.properties.get(project_first, "") or "").strip()
    last = (record.properties.get(project_last, "") or "").strip()
    if not first or not last or not ws.folder_id:
        return None
    author = folders.resolve(first, last, ws.folder_id, token, opener=opener)
    if not author:
        return None
    # Native mapping may point to the author folder.  Prefer its named Interior
    # Design child when present, while still accepting a direct folder mapping.
    children = drive.find_children(token, author, folders_only=True, opener=opener)
    wanted = " ".join(ws.corrections_folder_name.split()).casefold()
    hits = [f for f in children if " ".join(f.name.split()).casefold() == wanted]
    return hits[0].id if len(hits) == 1 else None


def _assets(token: str, folder_id: str, *, opener, root: Path,
            seen: set[str] | None = None, relative: Path = Path("."),
            capture: bool = False) -> list[Path]:
    """Download only conservative package asset trees.

    The INDD itself and proof PDFs remain separate inputs.  A native package's
    portable resources conventionally lives under ``Links`` and ``Document
    fonts``; preserving those relative directories lets the native adapter
    relink them without sweeping unrelated author files from the folder.
    """
    seen = seen or set()
    if folder_id in seen:
        return []
    seen.add(folder_id)
    found: list[Path] = []
    for entry in drive.list_folder(token, folder_id, opener=opener):
        if entry.is_folder:
            child = " ".join(entry.name.split()).casefold()
            keep = capture or child in {"links", "document fonts", "document_fonts", "documentfonts", "fonts"}
            # Once inside a package root, only named resource trees are
            # allowed. Do not recursively inspect Cover Design or arbitrary
            # nested folders just because they contain an image.
            if not keep:
                continue
            folder_name = "Document fonts" if child == "fonts" else _safe(entry.name)
            found.extend(_assets(token, entry.id, opener=opener, root=root,
                                 seen=seen, relative=relative / folder_name,
                                 capture=keep))
            continue
        if not capture:
            # Some designers leave a linked PNG beside the INDD rather than in
            # Links.  Carry image/font resources at the package root, while
            # ignoring unrelated proofs and prior deliverables.
            if Path(entry.name).suffix.casefold() not in {
                    ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff",
                    ".psd", ".ai", ".eps", ".otf", ".ttf", ".woff", ".woff2"}:
                continue
        dest = root / relative / _safe(entry.name)
        try:
            found.append(drive.download(token, entry.id, dest, opener=opener))
        except Exception as exc:  # noqa: BLE001 - caller records technical block
            raise NativeCorrectionError(f"could not download {entry.name}: {exc}") from exc
    return found


def _asset_sibling_folders(token: str, ws, record, folder_id: str, *, opener) -> list[str]:
    """Find a narrowly scoped sibling Fonts folder for a resolved book.

    Production author folders keep ``Fonts`` beside ``Interior Design``. A
    native package must carry those fonts, but sweeping ``Cover Design`` or
    other author material into the job would turn unrelated files into input.
    The metadata lookup is best effort because a mapped folder may be a direct
    child with no readable parent; the actual book folder remains sufficient.
    """
    parent_ids: list[str] = []
    mapped = bool(ws.corrections_native_folder_property)
    if not mapped:
        project_first, project_last = _project_name_properties(ws)
        first = str(record.properties.get(project_first, "") or "").strip()
        last = str(record.properties.get(project_last, "") or "").strip()
        if first and last and ws.folder_id:
            author = folders.resolve(first, last, ws.folder_id, token, opener=opener)
            if author:
                parent_ids.append(author)
    if not parent_ids:
        # Drive metadata includes parents even when the folder was supplied by
        # an explicit CRM mapping. This is a read-only request and failures
        # merely mean there are no extra sibling assets to stage.
        try:
            params = {"fields": "parents", **drive.SHARED_DRIVE_LIST}
            request = drive._request(drive._url(f"{drive.API}/files/{folder_id}", params), token)
            answer = drive._json_call(request, opener=opener, what="read the native folder parent")
            parent_ids.extend(str(parent) for parent in answer.get("parents") or [] if parent)
        except Exception:  # noqa: BLE001 - optional package asset lookup
            pass
    sibling_ids: list[str] = []
    for parent_id in dict.fromkeys(parent_ids):
        for entry in drive.find_children(token, parent_id, folders_only=True, opener=opener):
            if " ".join(entry.name.split()).casefold() in {"fonts", "document fonts", "document_fonts", "documentfonts"}:
                if entry.id != folder_id and entry.id not in sibling_ids:
                    sibling_ids.append(entry.id)
    return sibling_ids


def _artifact_paths(result: dict, source: Path, out_dir: Path, next_name: str) -> list[tuple[Path, str]]:
    paths: list[tuple[Path, str]] = []
    values = (("output_indd", next_name),
              ("output_pdf", Path(next_name).with_suffix(".pdf").name),
              ("output_package", Path(next_name).with_suffix(".zip").stem + " - package.zip"),
              ("report", Path(next_name).with_suffix(".report.json").name),
              ("report_path", Path(next_name).with_suffix(".report.json").name),
              ("audit_spreadsheet", Path(next_name).with_suffix(".corrections.xlsx").name))
    for key, fallback in values:
        value = result.get(key)
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = out_dir / path
        try:
            path.resolve().relative_to(out_dir.resolve())
        except ValueError:
            continue
        if path.is_file() and not any(existing == path for existing, _ in paths):
            paths.append((path, str(fallback)))
    # The root workflow may expose a manifest instead of individual keys.
    for value in result.get("artifacts", ()) or ():
        path = Path(value.get("path", "")) if isinstance(value, dict) else Path(str(value))
        if not path.is_absolute():
            path = out_dir / path
        try:
            path.resolve().relative_to(out_dir.resolve())
        except ValueError:
            continue
        if path.is_file() and not any(existing == path for existing, _ in paths):
            paths.append((path, path.name))
    return paths


def _write_minimal_audit(job_dir: Path, job: dict, *, status: str, reason: str) -> None:
    """Persist an early receipt without replacing a completed workflow result."""
    existing = job.get("result")
    if isinstance(existing, dict):
        if existing.get('status') in {'verified', 'designer_needed', 'clarification_needed'}:
            return
    result = {"status": status, "needs_designer": None, "reasons": [reason],
              "job_dir": str(job_dir), "output_indd": "", "output_pdf": "",
              "output_idml": ""}
    try:
        from docproof.interior.audit import write_audit
    except ImportError:
        return
    try:
        audit = write_audit(job_dir / "output", result, plan=None, context=job)
    except Exception as exc:  # noqa: BLE001 - preserve the original native receipt
        log.warning("Could not write early native audit for %s: %s", job.get("job_id"), exc)
        return
    if audit:
        result["audit_spreadsheet"] = str(Path(audit).resolve())
        job["result"] = result


def _call_workflow(source: Path, attachments: list[Path], text: str,
                   work_dir: Path, rules: dict) -> dict:
    try:
        from docproof.interior.workflow import run_local
    except ImportError as exc:
        raise NativeCorrectionError("native corrections workflow is unavailable") from exc
    result = run_local(source, attachments, text, work_dir, rules=rules)
    if not isinstance(result, dict):
        raise NativeCorrectionError("native corrections workflow returned no manifest")
    return result


def _status_props(ws, result: dict, *, output_names: list[str]) -> dict[str, str]:
    status = str(result.get("status", "technical_block"))
    props: dict[str, str] = {}
    status_property = ws.corrections_native_status_property or ws.hubspot_status_property
    if status_property:
        values = {
            "verified": ws.corrections_native_verified_value,
            "designer_needed": ws.corrections_native_designer_value,
            "clarification_needed": ws.corrections_native_clarification_value,
            "technical_block": ws.corrections_native_technical_value,
        }
        if values.get(status):
            props[status_property] = values[status]
    if ws.corrections_native_designer_property and result.get("needs_designer") in (True, False):
        value = result.get("needs_designer")
        props[ws.corrections_native_designer_property] = (
            "true" if value is True else "false" if value is False else "unknown")
    reasons = result.get("reasons") or result.get("reason") or result.get("designer_reason") or ""
    reason = "; ".join(str(item) for item in reasons) if isinstance(reasons, (list, tuple)) else str(reasons)
    if ws.corrections_native_reason_property and reason:
        props[ws.corrections_native_reason_property] = str(reason)[:500]
    if ws.corrections_native_output_property and output_names:
        props[ws.corrections_native_output_property] = ";".join(output_names)
    return props


def _record_identity(record, marker: str, urls: list[str], text: str) -> str:
    body = json.dumps({"record": record.id, "marker": marker,
                       "urls": urls, "text": hashlib.sha256(text.encode()).hexdigest()},
                      sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()


def _discover(drive_token: str, hs_token: str, ws, *, opener, report,
              home: Path | None = None) -> list[tuple[Any, str, NativeSource, list[DriveFile]]]:
    status_property = ws.corrections_native_status_property or ws.hubspot_status_property
    project_first, project_last = _project_name_properties(ws)
    want = [p for p in (status_property, ws.hubspot_first_property,
                        ws.hubspot_last_property, ws.corrections_native_folder_property,
                        ws.corrections_native_submission_property,
                        ws.hubspot_corrections_file_property,
                        ws.hubspot_corrections_text_property) if p]
    form_rows: list[dict] = []
    if ws.corrections_native_form_poll:
        # No property means the CRM record alone cannot identify which form
        # submission is current. Poll the explicitly configured form and freeze
        # its submittedAt/conversionId marker in the native job manifest.
        form_rows = form_submissions(hs_token, ws.corrections_native_form_id,
                                     opener=opener)
        form_rows = [row for row in form_rows
                     if _timestamp_value(str(row.get("submittedAt", "") or ""))
                     > _timestamp_value(ws.corrections_native_start_after)]
        if not form_rows:
            return []
        # Form-driven mode is event-triggered and cannot depend on a status
        # value that may not exist on the Projects dropdown.
        ready = _project_records(hs_token, ws, opener=opener)
    else:
        # Property mode retains the legacy Ready for Corrections gate.
        ready = hubspot.find_by_value(hs_token, ws.hubspot_object, status_property,
                                      ws.hubspot_corrections_ready_value,
                                      want_properties=want, opener=opener)
    works = []
    matched_markers: set[str] = set()
    seen_identities: set[str] = set()
    author_counts: dict[tuple[str, str], int] = {}
    for record in ready:
        key = (str(record.properties.get(project_first, "") or "").strip().casefold(),
               str(record.properties.get(project_last, "") or "").strip().casefold())
        author_counts[key] = author_counts.get(key, 0) + 1
    event_counts: dict[str, int] = {}
    if ws.corrections_native_form_poll:
        for record in ready:
            for event in _events_for(record, form_rows, ws):
                event_counts[_event_key(event)] = event_counts.get(_event_key(event), 0) + 1
    for record in ready:
        pre_events = _events_for(record, form_rows, ws) if ws.corrections_native_form_poll else None
        if ws.corrections_native_form_poll and not pre_events:
            continue
        folder_id = _folder_for(drive_token, ws, record, opener=opener)
        if not folder_id:
            report.needs_human.append((record.id, "the native book folder could not be resolved"))
            report.waiting += 1
            continue
        listing = drive.list_folder(drive_token, folder_id, opener=opener)
        first = record.properties.get(project_first, "")
        last = record.properties.get(project_last, "")
        surname = last.strip()
        if not surname:
            report.needs_human.append((record.id, "the HubSpot record has no surname for native Book N matching"))
            report.waiting += 1
            continue
        source, why = pick_source(listing, surname)
        if source is None:
            report.needs_human.append((record.id, "native Interior Design exports are missing or ambiguous" if why == "tie" else "no native Book N.indd export is ready"))
            report.waiting += 1
            continue
        if (ws.corrections_native_form_poll
                and not ws.corrections_native_submission_property
                and not (ws.corrections_native_form_file_property
                         or ws.corrections_native_form_notes_property)):
            report.needs_human.append((record.id, "configure the HubSpot file or notes property used by the corrections form"))
            report.waiting += 1
            continue
        key = (first.strip().casefold(), last.strip().casefold())
        if (ws.corrections_native_form_poll
                and not ws.corrections_native_submission_property
                and author_counts.get(key, 0) > 1 and not any(
                    str(row.get("recordId") or row.get("objectId") or "") == record.id
                    for row in form_rows)
                and not (ws.corrections_native_form_book_property
                         and ws.corrections_native_project_book_property
                         and pre_events)):
            report.needs_human.append((record.id, "multiple ready HubSpot records share this author; form submission association is ambiguous"))
            report.waiting += 1
            continue
        events = (pre_events if ws.corrections_native_form_poll else
                  [([], "", str(record.properties.get(
                      ws.corrections_native_submission_property, "") or ""))])
        if ws.corrections_native_form_poll and not ws.corrections_native_submission_property and not any(events):
            report.needs_human.append((record.id, "the configured corrections form has no submission for this HubSpot record"))
            report.waiting += 1
            continue
        for event in events:
            event_key = _event_key(event)
            associated = any(str(row.get("recordId") or row.get("objectId") or "") == record.id
                             for row in form_rows if str(row.get("submittedAt") or "") == event[2].split("|", 1)[0])
            if ws.corrections_native_form_poll and event_counts.get(event_key, 0) != 1 and not associated:
                report.needs_human.append((record.id, "the form submission matches multiple Projects; add a verified Project association or book title"))
                report.waiting += 1
                continue
            if home is not None:
                identity = _record_identity(record, event[2], event[0], event[1])
                # This event was matched to this Project even when its job is
                # already complete. Keep it out of native-intake.json's
                # attention queue on later polling ticks.
                matched_markers.add(event[2])
                if identity in seen_identities:
                    continue
                seen_identities.add(identity)
                finished = next((item for item in _read_jobs(home)
                                 if item.get("identity") == identity
                                 and _job_delivery_complete(item, ws)), None)
                if finished is not None:
                    continue
                prior = next((item for item in _read_jobs(home)
                              if item.get("identity") == identity), None)
                if prior is not None and (prior.get("blocked")
                                          or prior.get("held")
                                          or (prior.get("status") == "awaiting_attachment"
                                              and not _manual_files_ready(prior, home))
                                          or prior.get("status") in {"technical_block", "clarification_needed"}):
                    continue
            works.append((record, folder_id, NativeSource(source, versioned_name(source.name)[1], surname), listing, event))
    if ws.corrections_native_form_poll and home is not None:
        unmatched = [row for row in form_rows if _row_marker(row) not in matched_markers]
        if unmatched:
            report.needs_human.extend((str(row.get("id") or row.get("submittedAt") or "form submission"),
                                       "form submission could not be associated with exactly one Project")
                                      for row in unmatched)
        _write_json(home / "native-intake.json", {"form_id": ws.corrections_native_form_id,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "submissions": form_rows, "unmatched": unmatched})
    if ws.corrections_native_form_poll:
        # A single tick processes one item, so a stable global order matters
        # when several authors submitted at once; project API order is not
        # submission order.
        works.sort(key=lambda item: _timestamp_value(
            str(item[4][2]).split("|", 1)[0]))
    return works


def _pending_jobs(drive_token: str, home: Path, ws, *, opener) -> list[tuple[Any, str, NativeSource, list[DriveFile], tuple[list[str], str, str]]]:
    """Recover delivery/CRM work even after the ready dropdown moved on."""
    works = []
    for job in _read_jobs(home):
        if job.get("blocked") or job.get("held"):
            continue
        if not ws.corrections_native_auto_upload and job.get("local_complete"):
            continue
        if job.get("status") == "awaiting_attachment":
            if not _manual_files_ready(job, home):
                continue
        elif job.get("status") not in {"verified", "designer_needed", "clarification_needed"}:
            continue
        expected = set((job.get("artifact_hashes") or {}).keys())
        uploaded = set((job.get("uploaded") or {}).keys())
        if expected and expected.issubset(uploaded) and (job.get("crm_written") or not ws.hubspot_write_back):
            continue
        folder_id = str(job.get("folder_id") or "")
        source_id = str(job.get("source_id") or "")
        if not folder_id or not source_id:
            continue
        listing = drive.list_folder(drive_token, folder_id, opener=opener)
        source = next((entry for entry in listing if entry.id == source_id), None)
        parsed = versioned_name(source.name) if source else None
        if source is None or parsed is None:
            continue
        record = hubspot.HubSpotRecord(str(job.get("record_id") or ""),
                                       dict(job.get("record_properties") or {}))
        event = (list(job.get("submission_urls") or []),
                 str(job.get("submission_text") or ""),
                 str(job.get("submission_marker") or ""))
        works.append((record, folder_id, NativeSource(source, parsed[1], parsed[0]), listing, event))
    return works


def run_stage(token: str, home: Path, ws, state, runner, store, *, mock: bool,
              opener, hs_token: str | None, report) -> None:
    """Run at most one native book at a time and resume its own job ledger."""
    if not ws.corrections_enabled or ws.corrections_engine != "native" or not hs_token:
        return
    from docproof.interior.remote_control import allowed
    if (Path(home) / 'interior-computer.json').exists() or not allowed(home):
        return
    if ws.corrections_native_form_poll:
        from . import native_intake
        try:
            native_intake.run_stage(token, home, ws, opener=opener,
                                    hs_token=hs_token, report=report, mock=mock)
        except hubspot.HubSpotAuthError:
            raise
        except Exception as exc:
            report.failed.append(('native corrections', str(exc)))
        return
    try:
        # Delivery retries outrank newly discovered books, but local hold-mode
        # results must not starve the queue while publishing is disabled.
        # Awaiting manual attachments must resume from the frozen job even in
        # local hold mode; completed local outputs remain excluded here.
        works = _pending_jobs(token, home, ws, opener=opener)
        if not works:
            works = _discover(token, hs_token, ws, opener=opener, report=report, home=home)
        if not works and ws.corrections_native_auto_upload:
            works = _pending_jobs(token, home, ws, opener=opener)
    except hubspot.HubSpotAuthError:
        raise
    except Exception as exc:  # transient CRM/Drive failure is technical
        report.failed.append(("native corrections", str(exc)))
        return
    if not works:
        return
    try:
        with FolderLock(home / "native_jobs"):
            _run_one(token, home, ws, works[0], mock=mock, opener=opener,
                     hs_token=hs_token, report=report)
    except FolderInUse as exc:
        report.failed.append(("native corrections", str(exc)))


def _run_one(token: str, home: Path, ws, work, *, mock: bool, opener,
             hs_token: str, report) -> None:
    from docproof.interior import remote_control
    record, folder_id, source_info, listing, *event_data = work
    batch = event_data[1] if len(event_data) > 1 else None
    if batch:
        from . import native_queue
        native_queue.assert_active(home, batch['batch_id'])
    source = source_info.file
    urls, text, marker = _submission_properties(ws, record)
    # Pending jobs carry the frozen event even when CRM properties changed
    # after an interrupted upload. Never recompute their identity from the
    # mutable record.
    if event_data and event_data[0]:
        frozen_urls, frozen_text, frozen_marker = event_data[0]
        # Form events are complete immutable snapshots. Property-mode
        # discovery passes a synthetic marker-only event, so retain the CRM
        # file/text values on that first pass; pending jobs with frozen URLs or
        # text still take precedence over mutable CRM properties.
        if (ws.corrections_native_form_poll or frozen_urls or frozen_text):
            urls, text, marker = frozen_urls, frozen_text, frozen_marker
        elif frozen_marker:
            marker = frozen_marker
    job_key = _record_identity(record, marker, urls, text)
    jobs = [j for j in _read_jobs(home)
            if j.get("identity") == job_key and j.get("record_id") == record.id]
    job = jobs[0] if jobs else None
    # Once a submission is frozen, always resume its original source even if a
    # newer designer export has since become the lexically/highest numbered
    # file in Drive.
    if job is not None and job.get("source_id"):
        frozen = next((f for f in listing if f.id == job["source_id"]), None)
        if frozen is not None:
            parsed = versioned_name(frozen.name)
            if parsed:
                source = frozen
                source_info = NativeSource(frozen, parsed[1], parsed[0])
        else:
            report.needs_human.append((source.name, "the frozen source export is no longer in its Drive folder"))
            return
    if job is None:
        job_id = hashlib.sha256(job_key.encode()).hexdigest()[:24]
        job_dir = _job_dir(home, job_id)
        job = {"job_id": job_id, "identity": job_key, "record_id": record.id,
               "source_id": source.id, "source_name": source.name,
               "source_version": source_info.version,
               "source_fingerprint": _fingerprint(source),
               "source_remote_fingerprint": _fingerprint(source), "folder_id": folder_id,
               "submission_marker": marker, "submission_urls": urls,
               "submission_text": text, "record_properties": dict(record.properties),
               "status": "queued", "uploaded": {}, "created_at": datetime.now(timezone.utc).isoformat()}
        if batch:
            job.update(batch_id=batch['batch_id'], book_identity=batch['book'],
                       submission_receipts=batch['events'])
        _write_json(job_dir / "job.json", job)
    else:
        job_dir = _job_dir(home, job["job_id"])
    if job.get("blocked"):
        report.needs_human.append((source.name, str(job.get("reason") or job["blocked"])))
        return
    if mock:
        return
    try:
        source_dir = job_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        local_source = source_dir / _safe(job.get("source_name") or source.name)
        if batch and job.get('source_remote_fingerprint') != _fingerprint(source):
            raise native_queue.QueueError('The registered source changed after this batch began.')
        if local_source.is_file() and job.get("source_local_hash"):
            if _hash(local_source) != job["source_local_hash"]:
                raise NativeCorrectionError("the frozen local source was changed; restore it before retrying")
        else:
            local_source = drive.download(token, source.id, local_source, opener=opener)
            if batch and source.md5_checksum:
                with local_source.open('rb') as stream:
                    actual_md5 = hashlib.file_digest(stream, 'md5').hexdigest()
                if actual_md5 != source.md5_checksum:
                    raise native_queue.QueueError('The downloaded InDesign file does not match its frozen Drive checksum.')
            job["source_local_hash"] = _hash(local_source)
            job["source_remote_fingerprint"] = _fingerprint(source)
            _write_json(job_dir / "job.json", job)
        source_fingerprint = job.get("source_remote_fingerprint") or _fingerprint(source)
        # Attachments are all fetched, with stable names; no first-file
        # shortcut. A Files API permission error becomes a durable manual
        # attachment request, so the native workflow is never invoked with an
        # incomplete correction submission.
        cache_root = home / "manual-attachments"
        cached_file = getattr(native_files, "cached_file", None)
        manual_error_type = getattr(native_files, "ManualAttachmentRequired", ())
        saved_by_index = {
            str(index): str(path)
            for index, path in (job.get("submission_paths_by_index") or {}).items()
        }
        attachments: list[Path] = []
        missing: list[dict[str, str]] = []
        if batch:
            from .native_attachments import gather
            attachments, missing = gather(job, job_dir, urls, hs_token, home,
                                           opener=opener, save=lambda: _write_json(job_dir / 'job.json', job))
            saved_by_index = job.get('submission_paths_by_index', {})
        for index, url in enumerate([] if batch else urls):
            prior = Path(saved_by_index.get(str(index), ""))
            if prior.is_file():
                attachments.append(prior)
                continue
            cached = None
            if cached_file is not None:
                try:
                    cached = cached_file(url, cache_root)
                except Exception:  # noqa: BLE001 - cache miss is handled below
                    cached = None
            if cached is not None and Path(cached).is_file():
                target_dir = job_dir / "attachments" / str(index + 1)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / _safe(Path(cached).name)
                shutil.copy2(cached, target)
                attachments.append(target)
                saved_by_index[str(index)] = str(target)
                continue
            file_id = getattr(native_files, "file_id", lambda _url: None)(url)
            if file_id and (cache_root / str(file_id)).exists():
                raise NativeCorrectionError(
                    f"the cached manual attachment {file_id} failed integrity validation")
            try:
                target = native_files.download_file(
                    hs_token, url, job_dir / "attachments" / str(index + 1),
                    opener=opener, fallback_name=f"submission-{index + 1}")
                attachments.append(target)
                saved_by_index[str(index)] = str(target)
            except Exception as exc:  # noqa: BLE001 - classify manual bridge errors
                if manual_error_type and isinstance(exc, manual_error_type):
                    missing.append({
                        "file_id": str(getattr(exc, "file_id", "") or ""),
                        "filename": str(getattr(exc, "filename", "") or
                                         f"submission-{index + 1}"),
                        "url": str(url),
                    })
                    continue
                raise
        if missing:
            job.update({"status": "awaiting_attachment",
                        "missing_attachments": missing,
                        "submission_paths_by_index": saved_by_index,
                        "reason": "", "delivery_error": ""})
            _write_minimal_audit(
                job_dir, job, status="awaiting_attachment",
                reason="A submitted correction attachment is still required before native work can start.")
            _write_json(job_dir / "job.json", job)
            report.needs_human.append((source.name,
                                       "manual attachment upload is required before native corrections can run"))
            return
        job["submission_paths_by_index"] = saved_by_index
        job["submission_paths"] = [str(path) for path in attachments]
        job.pop("missing_attachments", None)
        job.pop("reason", None)
        _write_json(job_dir / "job.json", job)
        saved_assets = [Path(p) for p in job.get("asset_paths", [])]
        if saved_assets and all(path.is_file() for path in saved_assets):
            package_assets = saved_assets
        else:
            package_assets = _assets(token, folder_id, opener=opener, root=source_dir)
            # Fonts are commonly a sibling of Interior Design, while Links
            # and loose resources belong to that book folder itself.
            for sibling_id in _asset_sibling_folders(token, ws, record, folder_id, opener=opener):
                package_assets.extend(_assets(token, sibling_id, opener=opener,
                                              root=source_dir,
                                              relative=Path("Document fonts"),
                                              capture=True))
        job["asset_paths"] = [str(path) for path in package_assets]
        if batch:
            asset_hashes = {str(path): _hash(path) for path in package_assets}
            if 'asset_hashes' in job and job['asset_hashes'] != asset_hashes:
                raise native_queue.QueueError('A frozen font or linked book asset changed. The batch requires recovery.')
            job['asset_hashes'] = asset_hashes
            _write_json(job_dir / 'job.json', job)
        # A result is immutable once the local workflow has returned. A tick
        # that died during upload resumes from this manifest and never edits
        # the source a second time.
        if (job.get("status") in {"verified", "designer_needed",
                                   "clarification_needed"}
                and isinstance(job.get("result"), dict)):
            result = dict(job["result"])
            status = str(job["status"])
        else:
            rules = {"source_id": source.id, "source_fingerprint": source_fingerprint,
                     "submission_marker": marker, "job_id": job["job_id"],
                     "asset_paths": [str(path) for path in package_assets]}
            if batch:
                native_queue.assert_active(home, batch['batch_id'])
                rules['book_identity'] = batch['book']
            remote_control.require(home)
            result = _call_workflow(local_source, attachments, text, job_dir / "output", rules)
            status = str(result.get("status", "technical_block"))
            job.update({"status": status, "result": result,
                        "source_fingerprint": source_fingerprint})
            _write_json(job_dir / "job.json", job)
        # Re-list before any publish. A designer export arriving during the
        # local run must never be overwritten or receive the old result.
        current = next((f for f in drive.list_folder(token, folder_id, opener=opener)
                        if f.id == source.id), None)
        if current is None or _fingerprint(current) != source_fingerprint:
            job["blocked"] = "source_changed"
            job["reason"] = "the source InDesign export changed while corrections were running"
            _write_json(job_dir / "job.json", job)
            report.needs_human.append((source.name, job["reason"]))
            return
        artifacts = _artifact_paths(result, local_source, job_dir / "output",
                                    native_filename(source_info.surname, next_version(source_info.version)))
        if status == "technical_block" or (status == "clarification_needed" and not ws.corrections_native_partial_upload):
            details = result.get("reasons") or result.get("reason") or status
            details = "; ".join(map(str, details)) if isinstance(details, (list, tuple)) else str(details)
            if status == "technical_block":
                report.failed.append((source.name, details))
            else:
                report.needs_human.append((source.name, details))
            return
        roles = {name for _, name in artifacts}
        next_book = next_version(source_info.version)
        expected = {native_filename(source_info.surname, next_book),
                    native_filename(source_info.surname, next_book, ".pdf"),
                    native_filename(source_info.surname, next_book, ".report.json")}
        expected.add(native_filename(source_info.surname, next_book, ".corrections.xlsx"))
        if result.get("output_package"):
            expected.add(native_filename(source_info.surname, next_book, " - package.zip"))
        if not expected.issubset(roles):
            missing = sorted(expected - roles)
            job["status"] = "technical_block"
            job["reason"] = "native workflow did not produce required artifacts: " + ", ".join(missing)
            _write_json(job_dir / "job.json", job)
            report.failed.append((source.name, job["reason"]))
            return
        if status == "designer_needed" and not ws.corrections_native_partial_upload:
            job["held"] = "designer_needed"
            _write_json(job_dir / "job.json", job)
            report.needs_human.append((source.name, str(result.get("reason", "designer review is required"))))
            return
        names = [name for _, name in artifacts]
        hashes = {name: _hash(path) for path, name in artifacts}
        frozen_hashes = job.get("artifact_hashes") or {}
        if frozen_hashes and frozen_hashes != hashes:
            job["status"] = "technical_block"
            job["reason"] = "a completed native artifact changed before delivery"
            _write_json(job_dir / "job.json", job)
            report.failed.append((source.name, job["reason"]))
            return
        job["artifact_hashes"] = frozen_hashes or hashes
        if not ws.corrections_native_auto_upload:
            # A local hold is a completed, immutable delivery candidate. Keep
            # it out of ordinary discovery so later form submissions can run,
            # while retaining its manifest for a future explicit upload pass.
            job["local_complete"] = True
            _write_json(job_dir / "job.json", job)
            report.needs_human.append((source.name, "native correction outputs are ready locally; publishing is disabled"))
            return
        _write_json(job_dir / "job.json", job)
        # A Book N+1 with a different receipt is somebody else's work. Never
        # overwrite or upload beside it: the result needs a human decision.
        latest = drive.list_folder(token, folder_id, opener=opener)
        result_version = next_version(source_info.version)
        own_output_names = {
            native_filename(source_info.surname, result_version),
            native_filename(source_info.surname, result_version, ".pdf"),
            native_filename(source_info.surname, result_version, ".report.json"),
            native_filename(source_info.surname, result_version, " - package.zip"),
        }
        source_listing = [entry for entry in latest
                          if not (entry.name in own_output_names
                                  and entry.app_properties.get(NATIVE_JOB_PROP) == job["job_id"])]
        current_source, source_issue = pick_source(source_listing, source_info.surname)
        if current_source is None or current_source.id != source.id:
            reason = ("the native source version became ambiguous while corrections were running"
                      if source_issue == "tie" else
                      "a different highest native source export appeared while corrections were running")
            job["blocked"] = "source_changed"
            job["reason"] = reason
            _write_json(job_dir / "job.json", job)
            report.needs_human.append((source.name, reason))
            return
        newer_entry = newer_export(latest, source_info.surname, source_info.version)
        if newer_entry is not None:
            reason = f"a newer native export {newer_entry.name} appeared while corrections were running"
            job["blocked"] = "source_changed"
            job["reason"] = reason
            _write_json(job_dir / "job.json", job)
            report.needs_human.append((source.name, reason))
            return
        foreign = next((entry for entry in latest
                        if entry.name in names
                        and (entry.app_properties.get(NATIVE_JOB_PROP) != job["job_id"]
                             or entry.app_properties.get("docproof.native_hash")
                             != hashes.get(entry.name))), None)
        if foreign is not None:
            reason = f"Drive already contains {foreign.name} without this native job receipt"
            job["status"] = "clarification_needed"
            job["reason"] = reason
            _write_json(job_dir / "job.json", job)
            report.needs_human.append((source.name, reason))
            return
        for path, name in artifacts:
            if batch:
                native_queue.assert_active(home, batch['batch_id'])
            existing = next((f for f in latest if f.name == name and
                             f.app_properties.get(NATIVE_JOB_PROP) == job["job_id"]
                             and f.app_properties.get("docproof.native_hash") == hashes[name]), None)
            if existing is not None:
                job.setdefault("uploaded", {})[name] = existing.id
                continue
            remote_control.require(home)
            fid = drive.upload(token, folder_id, path, name=name,
                               mime_type="application/octet-stream",
                               app_properties={NATIVE_JOB_PROP: job["job_id"],
                                               NATIVE_SOURCE_PROP: source.id,
                                               NATIVE_MARKER: status,
                                               "docproof.native_hash": hashes[name]}, opener=opener)
            job.setdefault("uploaded", {})[name] = fid
            _write_json(job_dir / "job.json", job)
            report.uploaded.append(name)
        if status in {"verified", "designer_needed", "clarification_needed"} and not job.get("crm_written"):
            props = _status_props(ws, result, output_names=names)
            if props and ws.hubspot_write_back:
                hubspot.set_properties(hs_token, ws.hubspot_object, record.id, props,
                                       allow=set(props), opener=opener)
                job["crm_written"] = True
        job["status"] = status
        job.pop('delivery_error', None)
        _write_json(job_dir / "job.json", job)
        report.corrected.append(f"{source.name}: {status}")
    except remote_control.AutomationPaused:
        raise
    except Exception as exc:  # noqa: BLE001 - persisted technical block
        # Preserve a completed local result when delivery or CRM writeback
        # fails.  The next tick can adopt receipts and finish the external
        # boundary without invoking InDesign a second time.
        if batch and isinstance(exc, native_queue.QueueError):
            job['blocked'] = 'batch_integrity'
            job['reason'] = str(exc)
        elif (isinstance(job.get("result"), dict)
                and job.get("status") in {"verified", "designer_needed", "clarification_needed"}):
            job["delivery_error"] = str(exc)
        else:
            job["status"] = "technical_block"
            job["reason"] = str(exc)
        _write_minimal_audit(
            job_dir, job, status=str(job.get("status") or "technical_block"),
            reason=str(job.get("reason") or exc))
        _write_json(job_dir / "job.json", job)
        report.failed.append((source.name, str(exc)))


__all__ = ["NativeCorrectionError", "NativeSource", "versioned_name",
           "native_version", "pick_source", "pick_native_source", "newer_export", "run_stage"]

# Small descriptive aliases keep callers from depending on the legacy IDML
# helper's name while making the pure selection function convenient to test.
pick_native_source = pick_source
native_version = versioned_name
