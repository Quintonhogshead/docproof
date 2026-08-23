"""Galley memory — the compounding layer that persists across books.

A case file dies with its job; this package's :class:`MemoryStore` accumulates
across every book: house-style rulings, authors, and precedents. It is a single
SQLite file on the app volume (stdlib :mod:`sqlite3`, WAL mode).
"""

from galley.memory.store import (
    DEFAULT_DB_NAME,
    Author,
    MemoryStore,
    Precedent,
    Ruling,
    SEED_RULINGS,
)

__all__ = [
    "DEFAULT_DB_NAME",
    "Author",
    "MemoryStore",
    "Precedent",
    "Ruling",
    "SEED_RULINGS",
]
