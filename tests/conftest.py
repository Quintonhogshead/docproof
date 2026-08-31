"""Shared test plumbing: fixture .docx generation, a default config, and the
guarantee that none of this reaches the internet."""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from docproof.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
_CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"
_NEEDED = ("simple.docx", "styled.docx", "table.docx", "footnotes.docx",
           "googledoc.docx", "toc.docx", "tracked.docx", "textbox.docx")


def _ensure_fixtures() -> None:
    if all((FIXTURES / name).exists() for name in _NEEDED):
        return
    sys.path.insert(0, str(FIXTURES))
    try:
        import make_fixtures
        make_fixtures.simple()
        make_fixtures.styled()
        make_fixtures.table()
        make_fixtures.footnotes()
        make_fixtures.googledoc()
        make_fixtures.toc()
        make_fixtures.tracked()
        make_fixtures.textbox()
    finally:
        sys.path.remove(str(FIXTURES))


_ensure_fixtures()


@pytest.fixture(autouse=True)
def no_whole_book_cache(monkeypatch):
    """Every test re-reads; nothing is pinned between them.

    `load_config` fills in a default cache folder for the glossary, story sheet
    and continuity reads, which in a real run is the point — the same draft is
    not read twice. In the suite it would be a shared, persistent seam between
    tests: one test's fake glossary answered from another's run, provider call
    counts off by whatever the previous run happened to leave behind, and the
    developer's own ~/.docproof written to by a test. Empty means off (see
    docproof.config.default_cache_dir); a test about caching sets its own
    tmp_path."""
    monkeypatch.setenv("DOCPROOF_CACHE_DIR", "")


@pytest.fixture(autouse=True, scope="session")
def no_internet():
    """Nothing in this suite may leave the machine.

    Every network seam is injected — a fake provider, a fake opener — so this
    should never fire. It exists because the way you find out that one of
    those seams was missed is a default argument that bound the real function
    at import, a test that looks like it passes, and a vendor invoice. The
    loopback address stays open: the sign-in listener genuinely needs one, and
    127.0.0.1 goes nowhere.

    Held for the whole session rather than per test, because the calls this is
    meant to catch are exactly the ones a per-test guard misses: a worker
    thread that outlives the test that started it makes its vendor call after
    teardown has put the real socket back."""
    real = socket.socket.connect

    def guard(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(
                f"a test tried to reach {host} — every network call in "
                f"DocProof takes an injected opener or provider; pass one")
        return real(self, address, *args, **kwargs)

    socket.socket.connect = guard
    try:
        yield
    finally:
        socket.socket.connect = real


@pytest.fixture
def cfg():
    return load_config(_CONFIG)


@pytest.fixture(autouse=True)
def _forget_imaging_capabilities():
    """docproof.cover.imaging remembers, per process, which vendor parameter
    shape worked and whether streaming is supported — that memo is the point
    (it stops every image paying a rejected round trip), and it is exactly
    the kind of process-global state that makes one test's vendor answer
    change the next test's call count. Cleared around every test so each one
    probes from scratch."""
    from docproof.cover import imaging
    imaging.reset_capability_cache()
    yield
    imaging.reset_capability_cache()
