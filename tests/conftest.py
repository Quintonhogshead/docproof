"""Shared test plumbing: fixture .docx generation and a default config."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from docproof.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
_CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"
_NEEDED = ("simple.docx", "styled.docx", "table.docx", "footnotes.docx")


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
    finally:
        sys.path.remove(str(FIXTURES))


_ensure_fixtures()


@pytest.fixture
def cfg():
    return load_config(_CONFIG)
