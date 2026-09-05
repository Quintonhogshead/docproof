"""SQLite store for cross-book Galley precedents and rulings.

Unlike a per-book case file, this database accumulates house-style rulings and
precedents across runs.

Uses SQLite WAL mode with versioned, idempotent migrations. Callers inject
timestamps; ``export``/``restore`` provide a plain SQL backup path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_DB_NAME = "galley_memory.db"

# A fixed, deterministic default timestamp so a store opened without an injected
# clock still writes *something* stable rather than wall-clock noise. Callers
# that care about real time pass their own ``now``.
_DEFAULT_NOW = "1970-01-01T00:00:00Z"




@dataclass(frozen=True)
class Ruling:
    """A house-style rule. ``subject`` is its topic, ``rule`` the normative
    statement, ``source`` the cited authority (``"house"``, ``"CMOS"``)."""

    subject: str
    rule: str
    source: str = "house"
    created_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Author:
    name: str
    notes: str = ""
    created_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Precedent:
    """How one error type was ruled on one book, and why.

    Populated in bulk by a later ticket; this store just creates the table and
    supports insert + filtered query.
    """

    error_type: str
    find_text: str
    ruling: str  # accept | reject | downgrade | query | ...
    reason: str = ""
    book: str = ""
    ruled_by: str = ""
    created_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



# (subject, rule, source). ``subject`` is the stable unique key the seed is
# idempotent on (INSERT OR IGNORE against a UNIQUE index).
SEED_RULINGS: tuple[tuple[str, str, str], ...] = (
    (
        "NBSP-before-ellipsis",
        "Place a non-breaking space before an ellipsis; house style.",
        "house",
    ),
    (
        "unspaced em dash",
        "Use an unspaced em dash (word—word), no surrounding spaces; house style.",
        "house",
    ),
    (
        "number policy",
        "Spell out prose cardinals; residual prose cardinals auto-apply while "
        "labels, units, and ordinals remain queries ('Not applied & number policy').",
        "house",
    ),
    (
        "Chicago (CMOS) enforcement",
        "Enforce Chicago Manual of Style; there is no voice-preservation opt-out.",
        "CMOS",
    ),
)



Migration = Callable[[sqlite3.Connection, str], None]


def _migration_001_initial(conn: sqlite3.Connection, now: str) -> None:
    """Create the three tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rulings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            subject    TEXT NOT NULL,
            rule       TEXT NOT NULL,
            source     TEXT NOT NULL DEFAULT 'house',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_rulings_subject ON rulings(subject);

        CREATE TABLE IF NOT EXISTS authors (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            notes      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS precedents (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            error_type TEXT NOT NULL,
            find_text  TEXT NOT NULL DEFAULT '',
            ruling     TEXT NOT NULL,
            reason     TEXT NOT NULL DEFAULT '',
            book       TEXT NOT NULL DEFAULT '',
            ruled_by   TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_precedents_error_type
            ON precedents(error_type);
        """
    )


def _migration_002_seed_rulings(conn: sqlite3.Connection, now: str) -> None:
    """Seed the standing house-style rules. Idempotent via INSERT OR IGNORE on
    the UNIQUE(subject) index, so a re-run never duplicates a row."""
    conn.executemany(
        "INSERT OR IGNORE INTO rulings (subject, rule, source, created_at) "
        "VALUES (?, ?, ?, ?)",
        [(subject, rule, source, now) for subject, rule, source in SEED_RULINGS],
    )


# The list order IS the version order. Never reorder or delete; only append.
MIGRATIONS: tuple[Migration, ...] = (
    _migration_001_initial,
    _migration_002_seed_rulings,
)




class MemoryStore:
    """A single-writer SQLite memory store on the app volume."""

    def __init__(self, conn: sqlite3.Connection, now: Callable[[], str]) -> None:
        self._conn = conn
        self._now = now


    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        now: str | Callable[[], str] | None = None,
    ) -> "MemoryStore":
        """Open/create the DB at ``path``, set WAL, run migrations idempotently.

        ``path`` may be a directory (the app volume) — the default file name is
        appended — or a full file path. ``now`` is an injected clock: a fixed
        string, a callable returning a string, or ``None`` for a stable default.
        """
        clock = _as_clock(now)
        db_path = _resolve_path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        store = cls(conn, clock)
        store._migrate()
        return store

    def _migrate(self) -> None:
        """Apply any migrations past the DB's recorded ``user_version``."""
        cur = self._conn.execute("PRAGMA user_version")
        applied = int(cur.fetchone()[0])
        if applied >= len(MIGRATIONS):
            return
        with self._conn:  # one transaction; user_version bump is committed atomically
            for version in range(applied, len(MIGRATIONS)):
                MIGRATIONS[version](self._conn, self._now())
                # PRAGMA does not take a bound parameter; version is our own int.
                self._conn.execute(f"PRAGMA user_version = {version + 1}")

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


    def add_ruling(
        self, subject: str, rule: str, *, source: str = "house"
    ) -> Ruling:
        now = self._now()
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO rulings (subject, rule, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (subject, rule, source, now),
            )
        if cur.lastrowid and cur.rowcount:
            return Ruling(subject, rule, source, now, id=cur.lastrowid)
        # Subject already present: return the existing row rather than a phantom.
        row = self._conn.execute(
            "SELECT * FROM rulings WHERE subject = ?", (subject,)
        ).fetchone()
        return _ruling_from_row(row)

    def rulings(self) -> list[Ruling]:
        rows = self._conn.execute(
            "SELECT * FROM rulings ORDER BY id"
        ).fetchall()
        return [_ruling_from_row(r) for r in rows]


    def add_author(self, name: str, *, notes: str = "") -> Author:
        now = self._now()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO authors (name, notes, created_at) VALUES (?, ?, ?)",
                (name, notes, now),
            )
        return Author(name, notes, now, id=cur.lastrowid)

    def authors(self) -> list[Author]:
        rows = self._conn.execute("SELECT * FROM authors ORDER BY id").fetchall()
        return [_author_from_row(r) for r in rows]


    def add_precedent(
        self,
        error_type: str,
        find_text: str,
        ruling: str,
        *,
        reason: str = "",
        book: str = "",
        ruled_by: str = "",
        now: str | None = None,
    ) -> Precedent:
        # ``now`` pins ``created_at`` for a caller that dates its own records
        # (an archive ingest replaying an old job); the store's clock otherwise.
        now = now if now is not None else self._now()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO precedents "
                "(error_type, find_text, ruling, reason, book, ruled_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (error_type, find_text, ruling, reason, book, ruled_by, now),
            )
        return Precedent(
            error_type, find_text, ruling, reason, book, ruled_by, now,
            id=cur.lastrowid,
        )

    def precedents(self, *, error_type: str | None = None) -> list[Precedent]:
        if error_type is None:
            rows = self._conn.execute(
                "SELECT * FROM precedents ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM precedents WHERE error_type = ? ORDER BY id",
                (error_type,),
            ).fetchall()
        return [_precedent_from_row(r) for r in rows]


    def export(self, dest_path: str | Path) -> Path:
        """Write a restorable ``.sql`` dump (the nightly-to-Tigris hook, local).

        Uses :meth:`sqlite3.Connection.iterdump`, which emits the schema and every
        row as executable SQL. :meth:`restore` replays it into a fresh DB.
        """
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as fh:
            # iterdump emits schema + rows but not user_version; carry it so the
            # restored DB reports the same schema version and skips migrations.
            fh.write(f"PRAGMA user_version = {self.schema_version};\n")
            for line in self._conn.iterdump():
                fh.write(line)
                fh.write("\n")
        return dest

    @classmethod
    def restore(
        cls,
        dump_path: str | Path,
        dest_path: str | Path,
        *,
        now: str | Callable[[], str] | None = None,
    ) -> "MemoryStore":
        """Reconstruct a DB from a dump written by :meth:`export`.

        The dump already carries the schema, rows, and the ``user_version``
        pragma (iterdump emits it), so no migrations are re-run — the restored DB
        is row-for-row equivalent to the exported one.
        """
        clock = _as_clock(now)
        db_path = _resolve_path(dest_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            raise FileExistsError(f"restore target already exists: {db_path}")

        sql = Path(dump_path).read_text(encoding="utf-8")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            conn.executescript(sql)
        return cls(conn, clock)




def _resolve_path(path: str | Path) -> Path:
    """A directory (existing, or trailing-separator) gets the default file name."""
    p = Path(path)
    if p.is_dir() or str(path).endswith(("/", "\\")):
        return p / DEFAULT_DB_NAME
    return p


def _as_clock(now: str | Callable[[], str] | None) -> Callable[[], str]:
    if now is None:
        return lambda: _DEFAULT_NOW
    if callable(now):
        return now
    fixed = str(now)
    return lambda: fixed


def _ruling_from_row(row: sqlite3.Row) -> Ruling:
    return Ruling(
        subject=row["subject"],
        rule=row["rule"],
        source=row["source"],
        created_at=row["created_at"],
        id=row["id"],
    )


def _author_from_row(row: sqlite3.Row) -> Author:
    return Author(
        name=row["name"],
        notes=row["notes"],
        created_at=row["created_at"],
        id=row["id"],
    )


def _precedent_from_row(row: sqlite3.Row) -> Precedent:
    return Precedent(
        error_type=row["error_type"],
        find_text=row["find_text"],
        ruling=row["ruling"],
        reason=row["reason"],
        book=row["book"],
        ruled_by=row["ruled_by"],
        created_at=row["created_at"],
        id=row["id"],
    )


__all__ = [
    "DEFAULT_DB_NAME",
    "Author",
    "MemoryStore",
    "Precedent",
    "Ruling",
    "SEED_RULINGS",
]
