"""Pinned, local-only LanguageTool prerequisites and checker for fixed Galley.

Installation is an explicit build/setup operation on an already downloaded
archive. Checking a book never downloads a distribution, contacts a public
LanguageTool service, inherits HTTP proxies, or starts an arbitrary cached JAR.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import zipfile


LANGUAGETOOL_VERSION = "6.8"
WRAPPER_VERSION = "3.4.0"
ARCHIVE_URL = (
    "https://github.com/jxmorris12/language_tool_python/releases/download/"
    "LanguageTool-6.8/LanguageTool-6.8.zip")
# Published by the wrapper maintainer for its build of the official v6.8 source.
ARCHIVE_SHA256 = "6a7f6b67b779ae9505f7579f0c41453ea8d1bd72ae750bdc2c55ba974281467d"
# Canonical {relative file path: SHA-256} inventory of that verified archive.
# Binding the inventory itself prevents edited files plus an edited sidecar
# manifest from silently becoming an accepted new distribution.
DISTRIBUTION_SHA256 = "0313b2f301a3e23bdd3707596590fc77ac864cc9b261bab7bd1fb760731ac04e"
DEFAULT_HOME = Path("/opt/languagetool/LanguageTool-6.8")
JAVA_MINIMUM = 17
_JAVA_OPTIONS = frozenset({"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"})


class LocalRuntimeError(RuntimeError):
    """Required local checks are unavailable or did not finish completely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _distribution_hash(directory: Path) -> str:
    if not directory.is_dir() or directory.is_symlink():
        raise LocalRuntimeError(
            "The pinned LanguageTool 6.8 distribution is missing. Install the "
            "verified archive and set GALLEY_LANGUAGETOOL_HOME to its directory.")
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise LocalRuntimeError("The LanguageTool distribution contains an unsupported file.")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = _sha256(path)
    return hashlib.sha256(json.dumps(
        files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verified_directory(directory: Path) -> Path:
    if _distribution_hash(directory) != DISTRIBUTION_SHA256:
        raise LocalRuntimeError(
            "The LanguageTool distribution differs from the pinned 6.8 build. "
            "Reinstall the verified archive; no grammar check was performed.")
    return directory.resolve()


def _java_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _JAVA_OPTIONS}


def verify_languagetool_runtime(directory: str | Path | None = None) -> dict:
    """Read-only prerequisites and content identity; never starts or downloads LT."""
    try:
        wrapper = version("language_tool_python")
    except PackageNotFoundError as exc:
        raise LocalRuntimeError("Install Galley's languagetool dependency before running local checks.") from exc
    if wrapper != WRAPPER_VERSION:
        raise LocalRuntimeError(f"Galley requires language_tool_python {WRAPPER_VERSION}; found {wrapper}.")
    home = Path(directory or os.environ.get("GALLEY_LANGUAGETOOL_HOME") or DEFAULT_HOME)
    home = _verified_directory(home)
    java = shutil.which("java")
    if not java:
        raise LocalRuntimeError("LanguageTool requires Java 17 or newer; java is missing.")
    try:
        check = subprocess.run([java, "-version"], capture_output=True, text=True,
                               timeout=10, env=_java_environment(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalRuntimeError("Java could not be verified for the local grammar check.") from exc
    description = (check.stderr or check.stdout).strip()
    match = re.search(r'(?:openjdk|java) version "(\d+)(?:\.(\d+))?', description)
    major = int(match.group(1)) if match else 0
    if major == 1 and match:
        major = int(match.group(2) or "0")
    if check.returncode or major < JAVA_MINIMUM:
        raise LocalRuntimeError("LanguageTool requires a working Java 17 or newer installation.")
    return {"engine": "LanguageTool", "engine_version": LANGUAGETOOL_VERSION,
            "archive_sha256": ARCHIVE_SHA256, "distribution_sha256": DISTRIBUTION_SHA256,
            "wrapper_version": wrapper, "java_version": description,
            "java": java, "directory": str(home)}


def install_languagetool_archive(archive: str | Path, directory: str | Path) -> Path:
    """Explicit offline setup from the exact pinned ZIP; never downloads it."""
    archive, destination = Path(archive), Path(directory)
    if _sha256(archive) != ARCHIVE_SHA256:
        raise LocalRuntimeError("LanguageTool archive SHA-256 does not match the pinned release.")
    if destination.exists():
        return _verified_directory(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".languagetool-install-", dir=destination.parent) as temp:
        staging = Path(temp)
        prefix = f"LanguageTool-{LANGUAGETOOL_VERSION}"
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                path = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                        or not path.parts or path.parts[0] != prefix or stat.S_ISLNK(mode)):
                    raise LocalRuntimeError("LanguageTool archive has an unsafe entry.")
            package.extractall(staging)
        extracted = _verified_directory(staging / prefix)
        extracted.rename(destination)
    return destination.resolve()


@dataclass
class LocalMatch:
    """The fields consumed by DocProof's local LanguageTool adapter."""

    rule_id: str
    rule_issue_type: str
    offset: int
    error_length: int
    replacements: list[str]
    matched_text: str
    message: str


def _matches(response: object, text: str, dictionary: str) -> list[LocalMatch]:
    """Validate full completion before converting Java UTF-16 spans to Python."""
    if not isinstance(response, dict):
        raise LocalRuntimeError("LanguageTool returned an invalid response.")
    warnings = response.get("warnings")
    software = response.get("software")
    language = response.get("language")
    if (not isinstance(warnings, dict) or warnings.get("incompleteResults") is not False
            or any(value for key, value in warnings.items() if key != "incompleteResults")
            or any(response.get(key) for key in ("error", "errors", "partial", "incompleteResults"))):
        raise LocalRuntimeError("LanguageTool reported incomplete results; local coverage cannot be certified.")
    # This exact distribution was built without git-generated API metadata and
    # reports version=null. Its complete file inventory, checked before launch,
    # establishes the version; a different advertised version still fails.
    if (not isinstance(software, dict) or software.get("name") != "LanguageTool"
            or "version" not in software
            or software.get("version") not in (None, LANGUAGETOOL_VERSION)
            or software.get("apiVersion") != 1 or software.get("status") != ""
            or not isinstance(language, dict) or language.get("code") != dictionary
            or not isinstance(response.get("matches"), list)):
        raise LocalRuntimeError("LanguageTool returned an unexpected version, language, or match list.")
    positions = {0: 0}
    units = 0
    for index, char in enumerate(text, 1):
        units += 2 if ord(char) > 0xFFFF else 1
        positions[units] = index
    matches = []
    try:
        for item in response["matches"]:
            offset, length = item["offset"], item["length"]
            if type(offset) is not int or type(length) is not int or length < 0:
                raise ValueError("invalid span")
            start, end = positions[offset], positions[offset + length]
            rule = item["rule"]
            replacements = [replacement["value"] for replacement in item["replacements"]]
            fields = [rule["id"], rule["issueType"], item["message"], *replacements]
            if not all(isinstance(field, str) for field in fields):
                raise ValueError("invalid match text")
            matches.append(LocalMatch(rule["id"], rule["issueType"], start, end - start,
                                      replacements, text[start:end], item["message"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalRuntimeError("LanguageTool returned a malformed or unanchorable match.") from exc
    return matches


class PinnedLanguageTool:
    """One owned JVM, loopback HTTP only, no fallback, no mutable wrapper cache."""

    def __init__(self, dictionary: str):
        import requests

        if dictionary not in {"en-US", "en-GB", "en-CA", "en-AU", "en-NZ", "en-ZA"}:
            raise LocalRuntimeError(f"Unsupported fixed LanguageTool dictionary: {dictionary}.")
        self.runtime = verify_languagetool_runtime()
        self.dictionary = dictionary
        self.picky = False
        self._server = None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        self._url = f"http://127.0.0.1:{port}/v2/"
        command = [self.runtime["java"], "-Xmx768m", "-Dfile.encoding=UTF-8",
                   "-Duser.language=en", "-Duser.country=US", "-Duser.timezone=UTC",
                   "-Djava.net.preferIPv4Stack=true", "-cp",
                   str(Path(self.runtime["directory"]) / "languagetool-server.jar"),
                   "org.languagetool.server.HTTPServer", "--port", str(port)]
        try:
            self._server = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=_java_environment(),
                cwd=self.runtime["directory"])
            deadline = time.monotonic() + 30
            with requests.Session() as session:
                session.trust_env = False
                while time.monotonic() < deadline:
                    if self._server.poll() is not None:
                        raise LocalRuntimeError("The local LanguageTool server exited during startup.")
                    try:
                        with session.get(self._url + "healthcheck", timeout=2,
                                         allow_redirects=False) as response:
                            if response.status_code == 200:
                                return
                    except requests.RequestException:
                        pass
                    time.sleep(0.2)
            raise LocalRuntimeError("The local LanguageTool server did not start within 30 seconds.")
        except BaseException:
            self.close()
            raise

    def check(self, text: str) -> list[LocalMatch]:
        import requests

        if self._server is None or self._server.poll() is not None:
            raise LocalRuntimeError("The local LanguageTool server is not running.")
        params = {"language": self.dictionary, "text": text}
        if self.picky:
            params["level"] = "picky"
        try:
            # Sessions are request-local, so parallel batches share no mutable
            # HTTP or Unicode-offset state. Environment proxies are never used.
            with requests.Session() as session:
                session.trust_env = False
                with session.post(self._url + "check", data=params, timeout=(5, 300),
                                  allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise LocalRuntimeError("The local LanguageTool check failed; no result was accepted.")
                    payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise LocalRuntimeError("The local LanguageTool check failed; no result was accepted.") from exc
        return _matches(payload, text, self.dictionary)

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def pinned_languagetool(dictionary: str = "en-US") -> PinnedLanguageTool:
    return PinnedLanguageTool(dictionary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Install an already downloaded pinned archive")
    install.add_argument("--archive", type=Path, required=True)
    install.add_argument("--directory", type=Path, default=DEFAULT_HOME)
    commands.add_parser("verify", help="Verify installed dependencies without starting a server")
    commands.add_parser("smoke", help="Check a synthetic sentence using only the local server")
    args = parser.parse_args()
    if args.command == "install":
        print(install_languagetool_archive(args.archive, args.directory))
    elif args.command == "verify":
        print(json.dumps(verify_languagetool_runtime(), sort_keys=True))
    else:
        tool = pinned_languagetool()
        try:
            found = tool.check("This is an test.")
            if not any(match.matched_text == "an" and "a" in match.replacements for match in found):
                raise LocalRuntimeError("LanguageTool started but did not detect the synthetic smoke-test error.")
            print(json.dumps({"status": "ready", "engine_version": LANGUAGETOOL_VERSION,
                              "distribution_sha256": DISTRIBUTION_SHA256,
                              "synthetic_error_detected": True}, sort_keys=True))
        finally:
            tool.close()


if __name__ == "__main__":
    main()
