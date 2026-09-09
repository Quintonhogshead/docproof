"""Explicit-ID source collection. No surname or first-filename matching."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse
import zipfile

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.watch import drive, hubspot
from app.watch.settings import WatchSettings, folder_id_from
from app.settings import get_api_key
from docproof.promo.ingest import read_manuscript

PUBLIC_FIELDS = {"author_name", "book_title", "book_subtitle", "publication_date",
                 "book_description", "retailer_url"}
SOURCE_FIELDS = {"drive_folder_url", "manuscript_file_id", "cover_file_id", "portrait_file_id"}
MAX_MANUSCRIPT = 30 * 1024 * 1024
MAX_IMAGE = 12 * 1024 * 1024


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: str = "0-970"
    property_map: dict[str, str] = Field(default_factory=dict)
    ready_property: str = ""
    ready_value: str = ""
    website_url_property: str = "website_url"
    website_status_property: str = ""
    live_value: str = ""
    drive_folder_id: str = ""
    manuscript_file_id: str = ""
    cover_file_id: str = ""
    portrait_file_id: str = ""

    @field_validator("object_type", "ready_property", "website_url_property", "website_status_property")
    @classmethod
    def property_id(cls, value):
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("Use the internal property/object name, not a URL or display label.")
        return value

    @field_validator("property_map")
    @classmethod
    def mapped_properties(cls, value):
        if set(value) - PUBLIC_FIELDS - SOURCE_FIELDS:
            raise ValueError("Only approved public website fields and source file references may be mapped.")
        for key in value.values():
            cls.property_id(key)
        return {k: v for k, v in value.items() if v}

    @field_validator("drive_folder_id", "manuscript_file_id", "cover_file_id", "portrait_file_id")
    @classmethod
    def file_id(cls, value):
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
            raise ValueError("Use the exact Drive file or folder ID.")
        return value


class SourceError(ValueError):
    pass


def manuscript_bytes(name: str, body: bytes) -> tuple[str, str]:
    """Validate before extraction and return suffix + plain text."""
    if len(body) > MAX_MANUSCRIPT:
        raise SourceError("The manuscript exceeds the 30 MB upload limit.")
    suffix = Path(name).suffix.lower()
    if suffix == ".txt":
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceError("Save text manuscripts as UTF-8, or upload a DOCX.") from exc
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                if len(z.infolist()) > 10000 or sum(x.file_size for x in z.infolist()) > 150 * 1024 * 1024:
                    raise SourceError("The expanded Word document is too large.")
                if "word/document.xml" not in z.namelist():
                    raise SourceError("This file is not a Word document.")
            # Existing parser accepts a path, and performs tracked-change-aware extraction.
            import tempfile
            with tempfile.TemporaryDirectory(prefix="docproof-website-") as temp:
                path = Path(temp) / "book.docx"
                path.write_bytes(body)
                text = read_manuscript(path).text
        except (zipfile.BadZipFile, KeyError) as exc:
            raise SourceError("The Word document could not be read.") from exc
    else:
        raise SourceError("Upload a DOCX or UTF-8 TXT manuscript.")
    if not text.strip():
        raise SourceError("The manuscript contains no readable text.")
    return suffix, text


def image_bytes(body: bytes) -> tuple[bytes, str, str]:
    """Decode/re-encode uploads: no SVG/script, hidden metadata or trailing payloads."""
    if len(body) > MAX_IMAGE:
        raise SourceError("Images must be no larger than 12 MB.")
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(body)) as src:
            if src.format not in {"PNG", "JPEG", "WEBP"} or src.width * src.height > 36_000_000:
                raise SourceError("Use a PNG, JPEG or WebP image under 36 megapixels.")
            src.load()
            clean = ImageOps.exif_transpose(src).convert("RGBA" if src.mode in ("RGBA", "LA", "P") else "RGB")
            clean.thumbnail((3200, 3200))
            out = io.BytesIO()
            if clean.mode == "RGBA":
                clean.save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png", ".png"
            clean.save(out, format="JPEG", quality=90, optimize=True)
            return out.getvalue(), "image/jpeg", ".jpg"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise SourceError("This image could not be safely read.") from exc


def project_record(token: str, object_type: str, project_id: str, properties: list[str], *, opener=None):
    if not re.fullmatch(r"[0-9]+", project_id):
        raise SourceError("HubSpot project IDs must be numeric record IDs.")
    request = hubspot._request(f"{hubspot.API}/crm/v3/objects/{object_type}/{project_id}?" +
                               urlencode({"properties": ",".join(sorted(set(properties)))}), token)
    return hubspot._json_call(request, opener=opener or hubspot._open_url, what="read the website's project")


def collect_records(project: dict, *, token: str | None = None, opener=None) -> dict:
    cfg = SourceConfig.model_validate(project.get("source_config") or {})
    ids = project["hubspot_project_ids"]
    if not ids:
        raise SourceError("Link the exact HubSpot project ID before collecting sources.")
    if not cfg.property_map:
        raise SourceError("Configure the public HubSpot field mapping before collecting sources.")
    token = token or get_api_key("hubspot")
    if not token:
        raise SourceError("Configure the existing HubSpot credential before collecting sources.")
    wanted = list(cfg.property_map.values()) + ([cfg.ready_property] if cfg.ready_property else [])
    records = [project_record(token, cfg.object_type, i, wanted, opener=opener) for i in ids]
    normalized = []
    for expected, record in zip(ids, records):
        if str(record.get("id")) != expected or record.get("archived"):
            raise SourceError("HubSpot returned an unexpected or archived project.")
        props = record.get("properties", {})
        public = {logical: str(props.get(internal) or "") for logical, internal in cfg.property_map.items()
                  if logical in PUBLIC_FIELDS}
        normalized.append({"project_id": expected, **public})
    primary = records[0].get("properties", {})
    refs = {logical: str(primary.get(internal) or "") for logical, internal in cfg.property_map.items()
            if logical in SOURCE_FIELDS}
    return {"hubspot": {**normalized[0], "books": normalized}, "references": refs,
            "ready": bool(cfg.ready_property and cfg.ready_value and
                          primary.get(cfg.ready_property) == cfg.ready_value)}


def drive_token(watch_home: Path, *, opener=None) -> str:
    ws = WatchSettings.load(watch_home)
    refresh = get_api_key("google")
    if not refresh or not ws.client_id or not ws.client_secret:
        raise SourceError("Connect Google Drive in DocProof Automations before collecting book files.")
    return drive.refresh_access_token(ws.client_id, ws.client_secret, refresh,
                                     opener=opener or drive._open_url)


def get_drive_file(token: str, file_id: str, *, opener=None) -> dict:
    SourceConfig.file_id(file_id)
    req = drive._request(f"{drive.API}/files/{file_id}?" + urlencode({"fields":
                         "id,name,mimeType,modifiedTime,size,parents,trashed", "supportsAllDrives": "true"}), token)
    data = drive._json_call(req, opener=opener or drive._open_url, what="check the selected website file")
    if str(data.get("id")) != file_id or data.get("trashed"):
        raise SourceError("The selected Drive file is unavailable.")
    return data


def fetch_selected(token: str, file_id: str, *, folder_id: str = "", opener=None) -> tuple[dict, bytes]:
    """Follow exact nested parent IDs, rejecting files outside the configured author folder."""
    meta = get_drive_file(token, file_id, opener=opener)
    if folder_id:
        pending = list(meta.get("parents", []))
        seen = set()
        while pending and folder_id not in pending:
            candidate = pending.pop()
            if candidate in seen:
                continue
            seen.add(candidate)
            if len(seen) > 20:
                raise SourceError("Could not confirm the selected file belongs to the author folder.")
            pending.extend(get_drive_file(token, candidate, opener=opener).get("parents", []))
        if folder_id not in pending:
            raise SourceError("The selected file does not belong to this author's configured Drive folder.")
    if int(meta.get("size") or 0) > MAX_MANUSCRIPT:
        raise SourceError("The selected Drive file exceeds the upload size limit.")
    call = opener or drive._open_url
    if meta["mimeType"] == drive.GOOGLE_DOC_MIME:
        req = drive._request(f"{drive.API}/files/{file_id}/export?" + urlencode({"mimeType": drive.DOCX_MIME}), token)
        body = drive._call(req, opener=call, what="export the selected manuscript")
        meta = {**meta, "name": meta["name"] + ".docx"}
    else:
        body = drive.download_bytes(token, file_id, opener=call)
    if len(body) > MAX_MANUSCRIPT:
        raise SourceError("The selected Drive file exceeds the upload size limit.")
    return meta, body


def selected_refs(cfg: SourceConfig, refs: dict) -> dict:
    folder = cfg.drive_folder_id
    if not folder and refs.get("drive_folder_url"):
        raw = refs["drive_folder_url"]
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or parsed.hostname != "drive.google.com":
            raise SourceError("The mapped folder link is not a Google Drive URL.")
        folder = folder_id_from(raw)
    return {"folder_id": folder,
            **{key: getattr(cfg, key) or refs.get(key, "") for key in
               ("manuscript_file_id", "cover_file_id", "portrait_file_id")}}
