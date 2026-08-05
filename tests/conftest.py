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
           "googledoc.docx", "toc.docx", "tracked.docx")


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
    finally:
        sys.path.remove(str(FIXTURES))


_ensure_fixtures()


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Nothing in this suite may leave the machine.

    Every network seam is injected — a fake provider, a fake opener — so this
    should never fire. It exists because the way you find out that one of
    those seams was missed is a default argument that bound the real function
    at import, a test that looks like it passes, and a vendor invoice. The
    loopback address stays open: the sign-in listener genuinely needs one, and
    127.0.0.1 goes nowhere."""
    real = socket.socket.connect

    def guard(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(
                f"a test tried to reach {host} — every network call in "
                f"DocProof takes an injected opener or provider; pass one")
        return real(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.fixture
def cfg():
    return load_config(_CONFIG)
