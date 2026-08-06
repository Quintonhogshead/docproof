"""Provider API keys for the web build, kept on the volume.

The desktop app puts keys in the Mac Keychain. A Linux server has no Keychain —
its keyring backend does nothing — so the hosted build keeps the keys an
administrator sets in the portal in a small database beside the accounts, on the
same persistent volume, so they survive a redeploy. At startup the server loads
them into the environment, where `get_api_key` already looks first.

Only what an administrator typed here lives in this file. A key handed to the
server as an environment secret (`fly secrets set`, say) is never copied in — it
stays where it is, and a portal key simply takes precedence while it exists, so
removing the portal key brings the environment one back.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("docproof.app.keystore")


class KeyStore:
    """A tiny name→secret table. One row per provider that an administrator has
    set a key for. Every method opens its own connection, as the accounts store
    does, so the server's threads never share one."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS secrets ("
                         "name TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def get(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM secrets WHERE name = ?",
                              (name,)).fetchone()
        return row[0] if row else None

    def set(self, name: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO secrets (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value))

    def delete(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM secrets WHERE name = ?", (name,))

    def names(self) -> list[str]:
        with self._connect() as conn:
            return [r[0] for r in
                    conn.execute("SELECT name FROM secrets").fetchall()]
