from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from galley import local_runtime as runtime


def _payload(text="This is an test."):
    return {
        "software": {"name": "LanguageTool", "version": None, "apiVersion": 1, "status": ""},
        "language": {"code": "en-US"}, "warnings": {"incompleteResults": False},
        "matches": [{"offset": 8, "length": 2, "replacements": [{"value": "a"}],
                     "rule": {"id": "EN_A_VS_AN", "issueType": "misspelling"},
                     "message": "Use the correct article."}],
    }


def _fixture_distribution(tmp_path, monkeypatch):
    home = tmp_path / "LanguageTool-6.8"
    home.mkdir()
    (home / "languagetool-server.jar").write_bytes(b"trusted fixture executable")
    (home / "rules.txt").write_text("trusted fixture rules")
    monkeypatch.setattr(runtime, "DISTRIBUTION_SHA256", runtime._distribution_hash(home))
    monkeypatch.setenv("GALLEY_LANGUAGETOOL_HOME", str(home))
    monkeypatch.setattr(runtime, "version", lambda name: "3.4.0")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/trusted/java")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stderr='openjdk version "21.0.12"\nTest VM', stdout=""))
    return home


def test_runtime_content_identity_rejects_changed_added_and_missing_files(tmp_path, monkeypatch):
    home = _fixture_distribution(tmp_path, monkeypatch)
    verified = runtime.verify_languagetool_runtime()
    assert verified["engine_version"] == "6.8"
    assert verified["distribution_sha256"] == runtime.DISTRIBUTION_SHA256
    baseline = (home / "rules.txt").read_bytes()
    for mutation in ("changed", "added", "missing"):
        if mutation == "changed":
            (home / "rules.txt").write_bytes(b"different rules")
        elif mutation == "added":
            (home / "injected.jar").write_bytes(b"new executable")
        else:
            (home / "rules.txt").unlink()
        with pytest.raises(runtime.LocalRuntimeError, match="differs from the pinned"):
            runtime.verify_languagetool_runtime()
        (home / "rules.txt").write_bytes(baseline)
        (home / "injected.jar").unlink(missing_ok=True)


def test_missing_runtime_never_creates_cache_or_installs_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "version", lambda name: "3.4.0")
    monkeypatch.setenv("GALLEY_LANGUAGETOOL_HOME", str(tmp_path / "absent"))
    with pytest.raises(runtime.LocalRuntimeError, match="distribution is missing"):
        runtime.verify_languagetool_runtime()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("wrapper,java", [("3.5.0", "21"), ("3.4.0", "11")])
def test_runtime_refuses_unpinned_wrapper_or_old_java(tmp_path, monkeypatch, wrapper, java):
    _fixture_distribution(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "version", lambda name: wrapper)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stderr=f'openjdk version "{java}.0.1"', stdout=""))
    with pytest.raises(runtime.LocalRuntimeError):
        runtime.verify_languagetool_runtime()


def test_installer_requires_archive_digest_before_creating_destination(tmp_path):
    archive = tmp_path / "untrusted.zip"
    archive.write_bytes(b"not the pinned archive")
    destination = tmp_path / "installed" / "LanguageTool-6.8"
    with pytest.raises(runtime.LocalRuntimeError, match="archive SHA-256"):
        runtime.install_languagetool_archive(archive, destination)
    assert not destination.parent.exists()


def test_installer_verifies_extracted_inventory_and_reuses_exact_install(tmp_path, monkeypatch):
    archive = tmp_path / "fixture.zip"
    files = {"languagetool-server.jar": b"test jar", "rules/rules.txt": b"test rules"}
    with zipfile.ZipFile(archive, "w") as package:
        for name, content in files.items():
            package.writestr("LanguageTool-6.8/" + name, content)
    monkeypatch.setattr(runtime, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    inventory = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    monkeypatch.setattr(runtime, "DISTRIBUTION_SHA256", hashlib.sha256(json.dumps(
        inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    destination = tmp_path / "installed" / "LanguageTool-6.8"
    assert runtime.install_languagetool_archive(archive, destination) == destination.resolve()
    original_time = (destination / "languagetool-server.jar").stat().st_mtime_ns
    runtime.install_languagetool_archive(archive, destination)
    assert (destination / "languagetool-server.jar").stat().st_mtime_ns == original_time
    (destination / "rules/rules.txt").write_bytes(b"altered")
    with pytest.raises(runtime.LocalRuntimeError, match="differs from the pinned"):
        runtime.install_languagetool_archive(archive, destination)


def test_match_conversion_preserves_unicode_offsets_and_lengths():
    text = "😀 an 🐟"
    payload = _payload()
    payload["matches"][0].update(offset=3, length=2)
    payload["matches"].append({**payload["matches"][0], "offset": 6, "length": 2})
    first, second = runtime._matches(payload, text, "en-US")
    assert (first.offset, first.error_length, first.matched_text) == (2, 2, "an")
    assert (second.offset, second.error_length, second.matched_text) == (5, 1, "🐟")
    # No class-level Unicode state survives between documents.
    assert runtime._matches(_payload(), "This is an test.", "en-US")[0].offset == 8


@pytest.mark.parametrize("mutation", [
    lambda p: p["warnings"].update(incompleteResults=True),
    lambda p: p["warnings"].update(reason="timeout"),
    lambda p: p.pop("warnings"),
    lambda p: p.update(partial=True),
    lambda p: p.update(error="server failure"),
    lambda p: p["software"].update(version="6.9"),
    lambda p: p["software"].pop("version"),
    lambda p: p["language"].update(code="en-GB"),
    lambda p: p.update(matches=None),
    lambda p: p["matches"][0].update(offset=-1),
    lambda p: p["matches"][0].update(length=True),
    lambda p: p["matches"][0].update(replacements=[{"value": None}]),
])
def test_partial_or_malformed_response_never_becomes_a_clean_read(mutation):
    payload = _payload()
    mutation(payload)
    with pytest.raises(runtime.LocalRuntimeError):
        runtime._matches(payload, "This is an test.", "en-US")


def test_match_cannot_anchor_in_middle_of_surrogate_pair():
    payload = _payload()
    payload["matches"][0].update(offset=1, length=1)
    with pytest.raises(runtime.LocalRuntimeError, match="unanchorable"):
        runtime._matches(payload, "😀 text", "en-US")


def test_local_check_ignores_proxy_environment_and_redirects(monkeypatch):
    import requests

    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid")
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid")
    calls = []

    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def json(self): return _payload()

    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, self.trust_env, kwargs))
            return Response()

    monkeypatch.setattr(requests, "Session", Session)
    tool = runtime.PinnedLanguageTool.__new__(runtime.PinnedLanguageTool)
    tool._server = SimpleNamespace(poll=lambda: None)
    tool._url = "http://127.0.0.1:12345/v2/"
    tool.dictionary, tool.picky = "en-US", False
    assert tool.check("This is an test.")[0].replacements == ["a"]
    assert calls == [("http://127.0.0.1:12345/v2/check", False,
        {"data": {"language": "en-US", "text": "This is an test."},
         "timeout": (5, 300), "allow_redirects": False})]
    Response.status_code = 302
    with pytest.raises(runtime.LocalRuntimeError, match="no result was accepted"):
        tool.check("This is an test.")


def test_failed_server_start_is_bounded_and_cleans_up(monkeypatch):
    import requests

    monkeypatch.setattr(runtime, "verify_languagetool_runtime", lambda: {
        "java": "/trusted/java", "directory": "/trusted/LanguageTool-6.8"})

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): assert address == ("127.0.0.1", 0)
        def getsockname(self): return ("127.0.0.1", 12345)

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs): raise requests.ConnectionError("not ready")

    stopped = []
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append("terminated"),
                              wait=lambda timeout: stopped.append("waited"))
    monkeypatch.setattr(runtime.socket, "socket", lambda *a: Socket())
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(requests, "Session", Session)
    ticks = iter([0, 31])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(ticks))
    with pytest.raises(runtime.LocalRuntimeError, match="within 30 seconds"):
        runtime.pinned_languagetool()
    assert stopped == ["terminated", "waited"]


def test_hosted_build_installs_and_tests_the_pinned_runtime():
    root = Path(__file__).resolve().parents[1]
    docker = (root / "Dockerfile").read_text()
    import tomllib
    fly = tomllib.loads((root / "fly.toml").read_text())
    assert runtime.ARCHIVE_URL in docker
    assert "python -m galley.local_runtime install" in docker
    assert "python -m galley.local_runtime smoke" in docker
    assert fly["env"]["GALLEY_LANGUAGETOOL_HOME"] == str(runtime.DEFAULT_HOME)
    assert fly["env"]["LTP_PATH"] == str(runtime.DEFAULT_HOME.parent)
    assert fly["env"]["LTP_JAR_DIR_PATH"] == str(runtime.DEFAULT_HOME)
