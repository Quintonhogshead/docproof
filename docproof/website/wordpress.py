"""The deliberately small client for the DocProof WordPress bridge.

This module is the only part of the website workflow that knows the remote
WordPress API.  It accepts already-rendered public HTML: WordPress must never
interpret a SiteSpec, call a model, or receive private manuscript data.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


PAGES = ("home", "about", "books", "contact")
MAX_HTML_BYTES = 2_000_000
MAX_ASSET_BYTES = 20_000_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MEDIA_TYPE = re.compile(r"^(?:image/[A-Za-z0-9.+-]+|application/pdf)$")


class WordPressBridgeError(RuntimeError):
    """A bridge operation did not produce a trustworthy result."""


class WordPressConfigurationError(WordPressBridgeError):
    """The destination has not been configured for safe publication."""


class WordPressSecurityError(WordPressBridgeError):
    """A destination or release payload violates a local safety boundary."""


class WordPressRemoteError(WordPressBridgeError):
    """The configured bridge refused or could not complete a request."""


@dataclass(frozen=True)
class WordPressReceipt:
    """A durable remote fact suitable for binding to staff approval."""

    release_id: str
    state: str
    digest: str
    idempotency_key: str
    preview_url: str = ""
    active_release_id: str = ""
    raw: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "state": self.state,
            "digest": self.digest,
            "idempotency_key": self.idempotency_key,
            "preview_url": self.preview_url,
            "active_release_id": self.active_release_id,
            "raw": dict(self.raw or {}),
        }


def _safe_id(value: object, label: str) -> str:
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise WordPressSecurityError(f"{label} must be a safe ASCII identifier.")
    return text


def _public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_destination(url: str, *, allowed_hosts: Iterable[str] = (),
                         resolver: Callable[..., Any] = socket.getaddrinfo) -> str:
    """Return a normalized HTTPS origin after rejecting SSRF-shaped targets.

    A destination may additionally be pinned to a configured hostname.  DNS is
    checked immediately before a request so a hostname cannot quietly resolve
    to loopback or RFC1918 space after it was saved.
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise WordPressConfigurationError("WordPress destination URL is missing or too long.")
    parsed = urllib.parse.urlsplit(url.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise WordPressConfigurationError("WordPress destination has an invalid port.") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WordPressConfigurationError("WordPress destination must be an HTTPS origin without credentials.")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise WordPressConfigurationError("WordPress destination must be an origin, without a path or query.")
    if port not in (None, 443):
        raise WordPressConfigurationError("WordPress destination may use only HTTPS port 443.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise WordPressSecurityError("WordPress destination cannot be a local hostname.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WordPressSecurityError("WordPress destination must use a configured public hostname, not an IP address.")
    pinned = {str(item).rstrip(".").lower() for item in allowed_hosts if item}
    if pinned and host not in pinned:
        raise WordPressSecurityError("WordPress destination host is not one of this site's configured hosts.")
    try:
        answers = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WordPressConfigurationError(
            f"Could not resolve the configured WordPress host: {exc}") from exc
    addresses = {str(item[4][0]) for item in answers if len(item) >= 5 and item[4]}
    if not addresses or not all(_public_address(address) for address in addresses):
        raise WordPressSecurityError("WordPress destination resolves to a non-public address.")
    return f"https://{host}"


def _asset_bytes(asset: Mapping[str, Any]) -> bytes:
    value = asset.get("bytes", asset.get("data", b""))
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise WordPressSecurityError("Asset data must be bytes or base64.") from exc
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise WordPressSecurityError("Asset data must be bytes or base64.")


def release_digest(pages: Mapping[str, str], assets: Iterable[Mapping[str, Any]]) -> str:
    """A stable digest over the exact public bundle and immutable assets."""
    hasher = hashlib.sha256()
    for page in PAGES:
        value = pages.get(page)
        if not isinstance(value, str):
            raise WordPressSecurityError(f"Rendered bundle is missing the {page} page.")
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_HTML_BYTES:
            raise WordPressSecurityError(f"Rendered {page} page is too large.")
        hasher.update(page.encode("ascii") + b"\0" + encoded + b"\0")
    clean_assets = sorted(list(assets), key=lambda item: str(item.get("id", "")))
    for asset in clean_assets:
        asset_id = _safe_id(asset.get("id"), "asset id")
        data = _asset_bytes(asset)
        if len(data) > MAX_ASSET_BYTES:
            raise WordPressSecurityError(f"Asset {asset_id} is too large.")
        declared = str(asset.get("sha256", "")).lower()
        digest = hashlib.sha256(data).hexdigest()
        if declared and declared != digest:
            raise WordPressSecurityError(f"Asset {asset_id} hash does not match its bytes.")
        hasher.update(asset_id.encode("ascii") + b"\0" + digest.encode("ascii") + b"\0")
    return hasher.hexdigest()


def make_release_payload(release_id: str, pages: Mapping[str, str],
                         assets: Iterable[Mapping[str, Any]], idempotency_key: str,
                         spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate and encode a bridge payload without exposing local file paths."""
    assets = list(assets)
    release_id = _safe_id(release_id, "release id")
    idempotency_key = _safe_id(idempotency_key, "idempotency key")
    digest = release_digest(pages, assets)
    encoded_assets: list[dict[str, Any]] = []
    for asset in sorted(assets, key=lambda item: str(item.get("id", ""))):
        asset_id = _safe_id(asset.get("id"), "asset id")
        data = _asset_bytes(asset)
        media_type = str(asset.get("media_type", ""))
        if not _MEDIA_TYPE.fullmatch(media_type):
            raise WordPressSecurityError(f"Asset {asset_id} has an unsupported media type.")
        filename = str(asset.get("filename", ""))
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise WordPressSecurityError(f"Asset {asset_id} has an unsafe filename.")
        encoded_assets.append({
            "id": asset_id, "filename": filename, "media_type": media_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "alt": str(asset.get("alt", "")),
            "bytes_b64": base64.b64encode(data).decode("ascii"),
        })
    payload: dict[str, Any] = {
        "release_id": release_id,
        "idempotency_key": idempotency_key,
        "digest": digest,
        "pages": {page: pages[page] for page in PAGES},
        "assets": encoded_assets,
    }
    if spec is not None:
        # This is public-shaped audit data only. The bridge never renders it.
        payload["spec"] = dict(spec)
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _default_opener(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(_NoRedirect())
    return opener.open(request, timeout=timeout)


class WordPressBridge:
    """A site-scoped, injected-opener client for the maintained bridge plugin."""

    def __init__(self, base_url: str, site_id: str, username: str, password: str,
                 *, opener: Callable[..., Any] | None = None,
                 resolver: Callable[..., Any] = socket.getaddrinfo,
                 allowed_hosts: Iterable[str] = (), timeout: int = 20):
        if not username or not password:
            raise WordPressConfigurationError("WordPress bridge credentials are not configured.")
        self.base_url = validate_destination(base_url, allowed_hosts=allowed_hosts, resolver=resolver)
        self.site_id = _safe_id(site_id, "site id")
        self._username = username
        self._password = password
        # An injected opener is a test/transport seam and must have redirect
        # following disabled, just like _default_opener. We also reject any
        # response whose final URL differs as a defensive check.
        self._opener = opener or _default_opener
        self._resolver = resolver
        self.timeout = timeout

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/wp-json/docproof/v1/sites/{urllib.parse.quote(self.site_id)}/{suffix.lstrip('/')}"

    def _call(self, suffix: str, *, method: str = "GET", body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # Re-check before every network request to defend against DNS rebinding.
        validate_destination(self.base_url, resolver=self._resolver)
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self._url(suffix), data=data, method=method)
        credential = base64.b64encode(f"{self._username}:{self._password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {credential}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=self.timeout) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else ""
                if final_url and final_url != request.full_url:
                    raise WordPressSecurityError(
                        "WordPress bridge redirected an authenticated request.")
                status = getattr(response, "status", response.getcode())
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise WordPressSecurityError("WordPress bridge redirected an authenticated request.") from exc
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise WordPressRemoteError(f"WordPress bridge returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            raise WordPressRemoteError(f"Could not reach the WordPress bridge: {exc}") from exc
        if status < 200 or status >= 300:
            raise WordPressRemoteError(f"WordPress bridge returned HTTP {status}.")
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise WordPressRemoteError("WordPress bridge returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise WordPressRemoteError("WordPress bridge returned an unexpected response.")
        return parsed

    @staticmethod
    def _receipt(raw: Mapping[str, Any], *, fallback_release: str = "", fallback_key: str = "") -> WordPressReceipt:
        release_id = str(raw.get("release_id", fallback_release))
        digest = str(raw.get("digest", ""))
        if not release_id or not digest:
            raise WordPressRemoteError("WordPress bridge receipt omitted its immutable release identity.")
        return WordPressReceipt(release_id=release_id, state=str(raw.get("state", "")),
                                digest=digest, idempotency_key=str(raw.get("idempotency_key", fallback_key)),
                                preview_url=str(raw.get("preview_url", "")),
                                active_release_id=str(raw.get("active_release_id", "")), raw=raw)

    def stage_release(self, release_id: str, pages: Mapping[str, str], assets: Iterable[Mapping[str, Any]], *,
                      idempotency_key: str, spec: Mapping[str, Any] | None = None) -> WordPressReceipt:
        payload = make_release_payload(release_id, pages, assets, idempotency_key, spec)
        raw = self._call("releases", method="POST", body=payload)
        receipt = self._receipt(raw, fallback_release=release_id, fallback_key=idempotency_key)
        if receipt.digest != payload["digest"]:
            raise WordPressRemoteError("WordPress bridge staged a different rendered bundle.")
        return receipt

    def preview(self, release_id: str) -> dict[str, Any]:
        return self._call(f"releases/{_safe_id(release_id, 'release id')}/preview")

    def remote_pages(self, release_id: str, private: bool = True) -> dict[str, Any]:
        suffix = f"releases/{_safe_id(release_id, 'release id')}/preview" if private else "status"
        return self._call(suffix)

    def activate(self, release_id: str, *, expected_active_release_id: str, idempotency_key: str) -> WordPressReceipt:
        payload = {"release_id": _safe_id(release_id, "release id"),
                   "expected_active_release_id": _safe_id(expected_active_release_id or "none", "expected active release id"),
                   "idempotency_key": _safe_id(idempotency_key, "idempotency key")}
        return self._receipt(self._call("activate", method="POST", body=payload), fallback_release=release_id, fallback_key=idempotency_key)

    def rollback(self, release_id: str, *, expected_active_release_id: str, idempotency_key: str) -> WordPressReceipt:
        payload = {"release_id": _safe_id(release_id, "release id"),
                   "expected_active_release_id": _safe_id(expected_active_release_id or "none", "expected active release id"),
                   "idempotency_key": _safe_id(idempotency_key, "idempotency key")}
        return self._receipt(self._call("rollback", method="POST", body=payload), fallback_release=release_id, fallback_key=idempotency_key)

    def status(self) -> dict[str, Any]:
        return self._call("status")

    def reconcile(self, *, expected_active_release_id: str | None = None) -> dict[str, Any]:
        """Check that the remote site still has the expected managed surface.

        This is intentionally a query, never an automatic retry of a mutation:
        callers use it after a timeout or an uncertain activation response.
        """
        result = self.status()
        if result.get("site_id") != self.site_id or result.get("ownership_ok") is not True:
            raise WordPressRemoteError("WordPress bridge site binding or managed page ownership does not match.")
        if expected_active_release_id is not None and result.get("active_release_id") != expected_active_release_id:
            raise WordPressRemoteError("WordPress bridge active release does not match the expected release.")
        return result

    def health(self, expected_release_id: str | None = None) -> dict[str, Any]:
        suffix = "health" + ("?expected_release_id=" + urllib.parse.quote(_safe_id(expected_release_id, "release id")) if expected_release_id else "")
        result = self._call(suffix)
        if result.get("status") not in ("passed", "failed", "inconclusive"):
            raise WordPressRemoteError("WordPress bridge health response has no trustworthy status.")
        return result
