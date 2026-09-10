"""Safe HubSpot attachment downloads for the native corrections worker.

HubSpot form submissions expose a short lived redirect URL.  Resolving that
URL with a bearer token and letting a generic HTTP redirect handler continue
would risk sending the token to the redirect destination.  Native intake first
exchanges the numeric file id for a signed URL, then downloads that URL with a
fresh request that has no authorization headers.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .hubspot import HubSpotError

API = "https://api.hubapi.com"
_FORM_FILE = re.compile(
    r"^/form-integrations/v1/uploaded-files/signed-url-redirect/(?P<id>[0-9]+)(?:/)?$")
_PUBLIC_CDN = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:hubspotusercontent(?:[0-9]+|-[a-z0-9-]+)?|hubspot)\.net$")


def _domain(host: str, suffix: str) -> bool:
    """Match a domain and its subdomains, never a lookalike suffix."""
    return host == suffix or host.endswith("." + suffix)


def _hubspot_api_host(host: str) -> bool:
    return _domain(host, "hubapi.com") or _domain(host, "hubspot.com")


def _public_cdn_host(host: str) -> bool:
    return bool(_PUBLIC_CDN.fullmatch(host))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the bearer-token exchange from following a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ManualAttachmentRequired(HubSpotError):
    """The private app lacks ``files.ui_hidden.read`` for one form file."""

    def __init__(self, file_id: str, filename: str):
        self.file_id = file_id
        self.filename = filename
        super().__init__(
            "HubSpot requires the `files.ui_hidden.read` scope to fetch "
            f"attachment {file_id} ({filename}). Download that file manually "
            "and add it to DocProof's native attachment cache, then retry.")


class _StripHeaders(urllib.request.HTTPRedirectHandler):
    """Carry no request headers onto a signed-URL redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(newurl, method=req.get_method())


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)
_STRIP_HEADERS = urllib.request.build_opener(_StripHeaders)
# Identify the authorized desktop client instead of urllib's generic agent,
# which HubSpot's CDN rejects with Cloudflare error 1010. This is not an
# authentication header and contains no account or device information.
_STRIP_HEADERS.addheaders = [('User-Agent', 'DocProof/1.0 (+https://github.com/Quintonhogshead/docproof)')]


def _open_no_redirect(request: urllib.request.Request, timeout: int = 60):
    return _NO_REDIRECT.open(request, timeout=timeout)


def _open_stripped(request: urllib.request.Request, timeout: int = 60):
    return _STRIP_HEADERS.open(request, timeout=timeout)


class _PermissionDenied(HubSpotError):
    """Internal marker so only the signed-URL API 403 enables the bridge."""

    def __init__(self, error):
        self.error = error
        super().__init__(str(error))


@contextlib.contextmanager
def _answer(request, *, opener, what: str):
    try:
        with opener(request) as response:
            yield response
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise _PermissionDenied(exc) from exc
        if exc.code == 401:
            raise HubSpotError(
                "HubSpot rejected the native correction attachment request. "
                "Check the private-app token and its scopes. HubSpot said: "
                f"{getattr(exc, 'reason', 'permission denied')}."
            ) from exc
        raise HubSpotError(f"HubSpot answered {exc.code} while trying to {what}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HubSpotError(f"Could not {what}: {exc}. The next run will try again.") from exc


def _json_call(request, *, opener, what: str) -> dict:
    with _answer(request, opener=opener, what=what) as response:
        body = response.read()
    try:
        value = json.loads(body or b"{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HubSpotError(f"HubSpot sent an unreadable answer while trying to {what}.") from exc
    if not isinstance(value, dict):
        raise HubSpotError(f"HubSpot sent an unexpected answer while trying to {what}.")
    return value


def _file_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    match = _FORM_FILE.fullmatch(parsed.path)
    return match.group("id") if match else None


def file_id(url: str) -> str | None:
    """Return a numeric ID only for a recognized HubSpot form file URL."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname or not _hubspot_api_host(parsed.hostname.casefold()):
        return None
    return _file_id(url)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sanitize_name(name: str, fallback: str = "submission") -> str:
    value = Path(str(name).replace("\\", "/")).name
    value = re.sub(r"[\x00-\x1f\x7f]", "-", value).replace(":", "-")
    value = value.strip(" .")[:240]
    return value or fallback


def cached_file(url: str, cache_root) -> Path | None:
    """Return a manifest-verified manual attachment, if one is cached."""
    identifier = file_id(url)
    if not identifier:
        return None
    folder = Path(cache_root).resolve() / identifier
    manifest_path = folder / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        if not isinstance(manifest, dict) or str(manifest.get("file_id")) != identifier:
            return None
        filename = _sanitize_name(manifest.get("filename", ""))
        path = (folder / filename).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            return None
        expected = str(manifest.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or _sha256(path) != expected:
            return None
        return path
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def store_manual_file(source: Path, cache_root, file_id: str,
                      filename: str | None = None) -> Path:
    """Store one manually downloaded file in the immutable native cache."""
    identifier = str(file_id).strip()
    if not re.fullmatch(r"[0-9]+", identifier):
        raise HubSpotError("Manual native attachment IDs must be numeric HubSpot file IDs.")
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise HubSpotError(f"Manual native attachment {source} does not exist.")
    name = _sanitize_name(filename or source.name)
    root = Path(cache_root).expanduser().resolve()
    folder = root / identifier
    if folder.is_symlink():
        raise HubSpotError(f"Manual attachment cache directory {identifier} is a symlink.")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.is_symlink():
        raise HubSpotError(f"Manual attachment cache file {identifier}/{name} is a symlink.")
    digest = _sha256(source)
    manifest_path = folder / "manifest.json"
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HubSpotError(f"The manual attachment cache for {identifier} is unreadable.") from exc
        if (not isinstance(existing, dict)
                or str(existing.get("file_id")) != identifier
                or _sanitize_name(existing.get("filename", "")) != name
                or existing.get("sha256") != digest):
            raise HubSpotError(
                f"Manual attachment cache {identifier} already contains different bytes; "
                "remove that cache entry explicitly before replacing it.")
        if not target.is_file() or _sha256(target) != digest:
            raise HubSpotError(
                f"Manual attachment cache {identifier} was changed after it was stored.")
        return target
    if target.exists():
        if not target.is_file() or _sha256(target) != digest:
            raise HubSpotError(
                f"Manual attachment cache {identifier} already contains different bytes; "
                "remove that cache entry explicitly before replacing it.")
    if not target.exists():
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=".native-", dir=folder)
        os.close(temporary_fd)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if not target.is_file() or _sha256(target) != digest:
                    raise HubSpotError(
                        f"Manual attachment cache {identifier} already contains different bytes; "
                        "remove that cache entry explicitly before replacing it.")
        finally:
            temporary.unlink(missing_ok=True)
    manifest = {"file_id": identifier, "filename": name, "sha256": digest}
    temporary_manifest = folder / ".manifest.json.tmp"
    temporary_manifest.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    try:
        try:
            os.link(temporary_manifest, manifest_path)
        except FileExistsError:
            # Another writer completed the same immutable entry; recurse into
            # the normal verification path rather than overwriting its receipt.
            return store_manual_file(source, root, identifier, name)
    finally:
        temporary_manifest.unlink(missing_ok=True)
    return target


def _filename(url: str, disposition: str, fallback_name: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = (urllib.parse.parse_qs(parsed.query).get("filename") or [""])[0]
    if not name and disposition:
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition,
                          flags=re.I)
        if match:
            name = urllib.parse.unquote(match.group(1))
    if not name:
        name = urllib.parse.unquote(Path(parsed.path).name)
    # Treat both separators as untrusted input and remove control characters;
    # Path.name also prevents a URL filename from escaping the destination.
    return _sanitize_name(name, fallback_name)


def download_file(token: str, url: str, dest_dir, *, opener=_open_no_redirect,
                  fallback_name: str = "submission") -> Path:
    """Download one native form attachment without forwarding credentials."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HubSpotError("Native correction attachments must use an HTTPS HubSpot URL.")
    host = parsed.hostname.casefold()
    # The native adapter normally passes Drive's generic opener.  Do not let
    # that opener's redirect handler carry the bearer token across the signed
    # URL boundary; custom openers remain injectable for deterministic tests.
    from . import drive
    production_opener = opener in {_open_no_redirect, _open_stripped,
                                   drive._open_url}
    api_opener = _open_no_redirect if production_opener else opener
    file_id = _file_id(url) if _hubspot_api_host(host) else None
    if _hubspot_api_host(host):
        if not file_id:
            raise HubSpotError(
                "The native correction attachment URL is not a recognized HubSpot "
                "form file link with a numeric file ID.")
        request = urllib.request.Request(
            f"{API}/files/v3/files/{file_id}/signed-url", method="GET",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"})
        try:
            signed = _json_call(request, opener=api_opener,
                                what="resolve the native correction attachment")
        except _PermissionDenied as exc:
            raise ManualAttachmentRequired(
                file_id, _filename(url, "", fallback_name)) from exc
        signed_url = signed.get("url") or signed.get("signedUrl")
        if not isinstance(signed_url, str) or urllib.parse.urlparse(signed_url).scheme != "https":
            raise HubSpotError("HubSpot did not return a valid signed URL for the native correction attachment.")
    elif _public_cdn_host(host):
        signed_url = url
    else:
        raise HubSpotError("The native correction attachment is not on a recognized HubSpot host.")

    # This request intentionally contains no Authorization or other HubSpot
    # headers.  The default opener strips headers again on every redirect;
    # injected test openers see the same header-free request.
    request = urllib.request.Request(signed_url, method="GET")
    final_opener = _open_stripped if production_opener else opener
    try:
        with _answer(request, opener=final_opener, what="download the native correction attachment") as response:
            body = response.read()
            headers = getattr(response, "headers", None)
            disposition = headers.get("Content-Disposition", "") if headers is not None else ""
    except _PermissionDenied:
        query = urllib.parse.parse_qs(parsed.query)
        if not file_id or not all(query.get(key) for key in ('portalId', 'sign', 'conversionId', 'filename')):
            raise
        # A complete signed form link is a second documented download route.
        # The files API already authorized this file, but its CDN link failed.
        # Use the original form signature without any bearer token, including
        # on redirects. Do not enable this fallback for an unsigned URL.
        request = urllib.request.Request(url, method='GET')
        with _answer(request, opener=final_opener, what="download the signed form attachment") as response:
            body = response.read()
            headers = getattr(response, "headers", None)
            disposition = headers.get("Content-Disposition", "") if headers is not None else ""
    folder = Path(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _filename(url, disposition or "", fallback_name)
    # Authentication redirects may end in HTTP 200 with a login page while
    # retaining the filename from the original form link.
    if target.suffix.lower() in {'.docx', '.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff'}:
        lead = body.lstrip()[:200].lower()
        if lead.startswith((b'<!doctype html', b'<html')):
            raise HubSpotError('HubSpot returned an HTML sign-in page instead of the correction attachment.')
    target.write_bytes(body)
    return target
